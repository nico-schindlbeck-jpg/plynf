# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Self-serve accounts: email+password signup, API-key provisioning, plans.

This is the customer-facing account system behind the zero-terminal flow:

    sign up → (Stripe checkout) → API key → paste into agent → done.

Each account owns exactly one tenant and one live proxy API key
(``plynf_sk_live_…``). The proxy resolves keys against
``GET /internal/keys/{key}`` (shared-secret protected, short-TTL cached on
the proxy side), so a plan change through the Stripe webhook is live on the
proxy within a minute — no restart, no terminal.

Storage is a single JSON file with atomic writes (set
``PLINTH_DASHBOARD_ACCOUNTS_PATH``; empty = in-memory, used by tests and
the offline demo). One file is sufficient for a single-node control plane;
the store is intentionally interface-shaped so a Postgres backend can drop
in later without touching the API layer.

Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib, 200k iterations,
per-account salt). No plaintext ever touches disk.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

PLANS = ("free", "pro", "enterprise")
_PBKDF2_ITERATIONS = 200_000
_DUMMY_SALT = b"\x00" * 16  # for constant-time login on unknown emails
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VERIFY_TOKEN_TTL_S = 24 * 3600  # email-verification link lifetime
RESET_TOKEN_TTL_S = 3600  # password-reset link lifetime (deliberately short)


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    ).hex()


def new_api_key() -> str:
    return "plynf_sk_live_" + secrets.token_hex(24)


def new_tenant_id() -> str:
    # 128 bits — tenant ids appear in logs/headers, so make them
    # unguessable, not merely unique.
    return "t_" + secrets.token_hex(16)


def new_email_token() -> str:
    """High-entropy, URL-safe token for an email verification / reset link."""
    return secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    """Tokens are stored hashed — a leaked accounts table must not hand out
    working verify/reset links. The raw token lives only in the email."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AccountError(ValueError):
    """Raised for invalid signup/login/plan operations."""


def build_password_fields(password: str) -> dict[str, str]:
    """Salt + PBKDF2 hash for a new or changed password. The single source of
    the password rule, so signup and password-reset cannot drift apart."""
    if len(password) < 8:
        raise AccountError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    return {"password_salt": salt.hex(), "password_hash": _hash_password(password, salt)}


def build_new_account(email: str, password: str, plan: str = "free") -> dict[str, Any]:
    """Validate signup inputs and build a fresh (not-yet-stored) account dict.

    Shared by every storage backend (JSON file, Postgres) so the account shape
    + validation rules live in exactly one place. Always starts on the free
    plan; the desired tier is recorded as ``requested_plan`` and only activated
    through billing (an abandoned checkout never grants paid limits).
    """
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise AccountError("a valid email address is required")
    if plan not in PLANS:
        raise AccountError(f"unknown plan {plan!r}")
    return {
        "email": email,
        **build_password_fields(password),  # validates length, salts + hashes
        "tenant_id": new_tenant_id(),
        "api_key": new_api_key(),
        "plan": "free",
        "requested_plan": plan,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
        # Email verification + password reset (tokens stored hashed, see above).
        "verified": False,
        "verify_token_hash": None,
        "verify_token_expires": None,
        "reset_token_hash": None,
        "reset_token_expires": None,
    }


def account_public_view(account: dict[str, Any]) -> dict[str, Any]:
    """The shape returned to the browser — never hashes/salts."""
    return {
        "email": account["email"],
        "plan": account["plan"],
        "requested_plan": account.get("requested_plan"),
        "tenant_id": account["tenant_id"],
        "api_key": account["api_key"],
        "created_at": account["created_at"],
        "has_billing": bool(account.get("stripe_customer_id")),
        "verified": bool(account.get("verified", False)),
    }


def verify_password(account: dict[str, Any], password: str) -> bool:
    """Constant-time password check against a stored account dict."""
    expected = account["password_hash"]
    actual = _hash_password(password, bytes.fromhex(account["password_salt"]))
    return hmac.compare_digest(expected, actual)


# Column order for the Postgres backend / row<->dict mapping.
ACCOUNT_COLUMNS = (
    "email",
    "password_salt",
    "password_hash",
    "tenant_id",
    "api_key",
    "plan",
    "requested_plan",
    "created_at",
    "stripe_customer_id",
    "stripe_subscription_id",
    "verified",
    "verify_token_hash",
    "verify_token_expires",
    "reset_token_hash",
    "reset_token_expires",
)


class AccountStore:
    """Threadsafe account registry with optional JSON-file persistence."""

    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._accounts: dict[str, dict[str, Any]] = {}  # email → account
        self._by_key: dict[str, str] = {}  # api_key → email
        self._by_customer: dict[str, str] = {}  # stripe customer → email
        if self._path is not None and self._path.exists():
            self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        self._accounts = raw.get("accounts", {})
        self._reindex()

    def _reindex(self) -> None:
        self._by_key = {a["api_key"]: e for e, a in self._accounts.items()}
        self._by_customer = {
            a["stripe_customer_id"]: e
            for e, a in self._accounts.items()
            if a.get("stripe_customer_id")
        }

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"accounts": self._accounts}, indent=1, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    # -- public surface ------------------------------------------------------

    def create(self, email: str, password: str, plan: str = "free") -> dict[str, Any]:
        account = build_new_account(email, password, plan)
        email = account["email"]
        with self._lock:
            if email in self._accounts:
                raise AccountError("an account with this email already exists")
            self._accounts[email] = account
            self._reindex()
            self._save()
            return dict(account)

    def verify_login(self, email: str, password: str) -> dict[str, Any] | None:
        email = email.strip().lower()
        with self._lock:
            account = self._accounts.get(email)
            if account is None:
                # Hash anyway against a dummy salt so the response time does
                # not reveal whether the email exists (user enumeration).
                _hash_password(password, _DUMMY_SALT)
                return None
            return dict(account) if verify_password(account, password) else None

    def get(self, email: str) -> dict[str, Any] | None:
        with self._lock:
            a = self._accounts.get(email.strip().lower())
            return dict(a) if a else None

    def resolve_key(self, api_key: str) -> dict[str, Any] | None:
        """api_key → {tenant_id, tier, email} (the proxy's auth source)."""
        with self._lock:
            email = self._by_key.get(api_key)
            if email is None:
                return None
            a = self._accounts[email]
            return {"tenant_id": a["tenant_id"], "tier": a["plan"], "email": email}

    def rotate_key(self, email: str) -> dict[str, Any] | None:
        with self._lock:
            a = self._accounts.get(email.strip().lower())
            if a is None:
                return None
            a["api_key"] = new_api_key()
            self._reindex()
            self._save()
            return dict(a)

    def set_plan(self, email: str, plan: str) -> dict[str, Any] | None:
        if plan not in PLANS:
            raise AccountError(f"unknown plan {plan!r}")
        with self._lock:
            a = self._accounts.get(email.strip().lower())
            if a is None:
                return None
            a["plan"] = plan
            self._save()
            return dict(a)

    def attach_stripe(
        self,
        email: str,
        customer_id: str | None = None,
        subscription_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            a = self._accounts.get(email.strip().lower())
            if a is None:
                return None
            if customer_id:
                a["stripe_customer_id"] = customer_id
            if subscription_id:
                a["stripe_subscription_id"] = subscription_id
            self._reindex()
            self._save()
            return dict(a)

    def email_for_customer(self, customer_id: str) -> str | None:
        with self._lock:
            return self._by_customer.get(customer_id)

    # -- email verification / password reset --------------------------------

    def issue_verification(self, email: str) -> str | None:
        """Mint a verification token for an existing account (None if unknown).
        Returns the RAW token — the caller emails it; only its hash is stored."""
        token = new_email_token()
        with self._lock:
            a = self._accounts.get(email.strip().lower())
            if a is None:
                return None
            a["verify_token_hash"] = _hash_token(token)
            a["verify_token_expires"] = time.time() + VERIFY_TOKEN_TTL_S
            self._save()
        return token

    def verify_email(self, token: str) -> str | None:
        """Consume a verification token: flag the account verified and clear
        the token. Returns the email on success, None if invalid/expired."""

        def _mark(account: dict[str, Any]) -> None:
            account["verified"] = True

        return self._consume_token(token, "verify", on_match=_mark)

    def issue_password_reset(self, email: str) -> str | None:
        """Mint a reset token for an existing account (None if unknown). The
        caller must NOT reveal which, to avoid email enumeration."""
        token = new_email_token()
        with self._lock:
            a = self._accounts.get(email.strip().lower())
            if a is None:
                return None
            a["reset_token_hash"] = _hash_token(token)
            a["reset_token_expires"] = time.time() + RESET_TOKEN_TTL_S
            self._save()
        return token

    def reset_password(self, token: str, new_password: str) -> str | None:
        """Consume a reset token and set a new password. Returns the email on
        success, None if invalid/expired. Raises AccountError on a weak
        password (length checked before the token is spent)."""
        fields = build_password_fields(new_password)
        return self._consume_token(token, "reset", on_match=lambda a: a.update(fields))

    def _consume_token(self, token: str, kind: str, on_match) -> str | None:
        """Find the account whose ``{kind}_token_hash`` matches (constant-time),
        apply ``on_match`` if unexpired, then clear the token. Shared by the
        verify + reset paths so the match/expiry/clear logic lives once."""
        h = _hash_token(token)
        now = time.time()
        with self._lock:
            for email, a in self._accounts.items():
                stored = a.get(f"{kind}_token_hash")
                if not stored or not hmac.compare_digest(stored, h):
                    continue
                exp = a.get(f"{kind}_token_expires")
                if exp is None or now > float(exp):
                    return None
                on_match(a)
                a[f"{kind}_token_hash"] = None
                a[f"{kind}_token_expires"] = None
                self._save()
                return email
            return None

    def public_view(self, account: dict[str, Any]) -> dict[str, Any]:
        """The shape returned to the browser — never hashes/salts."""
        return account_public_view(account)


__all__ = [
    "ACCOUNT_COLUMNS",
    "PLANS",
    "RESET_TOKEN_TTL_S",
    "VERIFY_TOKEN_TTL_S",
    "AccountError",
    "AccountStore",
    "account_public_view",
    "build_new_account",
    "build_password_fields",
    "new_api_key",
    "new_email_token",
    "new_tenant_id",
    "verify_password",
]
