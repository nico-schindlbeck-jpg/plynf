# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Postgres-backed account store — the durable, backup-able production
counterpart to the single-JSON-file ``AccountStore``.

Drop-in: same interface as ``accounts.AccountStore`` (create / verify_login /
get / resolve_key / rotate_key / set_plan / attach_stripe / email_for_customer
/ public_view), so the API layer is backend-agnostic. Account shape,
validation and password hashing are shared via ``accounts.build_new_account``
/ ``verify_password`` / ``account_public_view`` — there is exactly one account
model regardless of backend.

Sync driver (psycopg3, one short-lived connection per op) to match the
synchronous store interface the FastAPI routes already call. Account ops are
infrequent (signup/login/rotate) and the proxy caches key resolution, so
connect-per-op is fine for a single-node control plane; swap in a pool if that
changes. psycopg is imported lazily so this module loads even where it isn't
installed (JSON-file deploys / tests that don't touch Postgres).
"""

from __future__ import annotations

import time
from typing import Any

from .accounts import (
    ACCOUNT_COLUMNS,
    PLANS,
    RESET_TOKEN_TTL_S,
    VERIFY_TOKEN_TTL_S,
    AccountError,
    _hash_token,
    account_public_view,
    build_new_account,
    build_password_fields,
    new_api_key,
    new_email_token,
    verify_password,
)

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS accounts (
        email                   TEXT PRIMARY KEY,
        password_salt           TEXT NOT NULL,
        password_hash           TEXT NOT NULL,
        tenant_id               TEXT NOT NULL,
        api_key                 TEXT NOT NULL UNIQUE,
        plan                    TEXT NOT NULL,
        requested_plan          TEXT,
        created_at              TEXT NOT NULL,
        stripe_customer_id      TEXT,
        stripe_subscription_id  TEXT,
        verified                BOOLEAN NOT NULL DEFAULT FALSE,
        verify_token_hash       TEXT,
        verify_token_expires    DOUBLE PRECISION,
        reset_token_hash        TEXT,
        reset_token_expires     DOUBLE PRECISION
    )
    """,
    # Forward-compat: upgrade a table first created before email flows existed
    # (Part A) without a migration tool. ADD COLUMN IF NOT EXISTS is idempotent.
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS verify_token_hash TEXT",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS verify_token_expires DOUBLE PRECISION",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS reset_token_hash TEXT",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS reset_token_expires DOUBLE PRECISION",
    "CREATE INDEX IF NOT EXISTS idx_accounts_api_key ON accounts (api_key)",
    "CREATE INDEX IF NOT EXISTS idx_accounts_stripe_customer ON accounts (stripe_customer_id)",
    # verify_email / reset_password look accounts up by token hash; without
    # these the consume path is a seq scan on every confirm.
    "CREATE INDEX IF NOT EXISTS idx_accounts_verify_token ON accounts (verify_token_hash)",
    "CREATE INDEX IF NOT EXISTS idx_accounts_reset_token ON accounts (reset_token_hash)",
)

_COLS = ", ".join(ACCOUNT_COLUMNS)
_PLACEHOLDERS = ", ".join(["%s"] * len(ACCOUNT_COLUMNS))

# Throwaway account used only to spend equal PBKDF2 time on an unknown email
# (anti-enumeration), mirroring AccountStore's dummy-salt behavior.
_DUMMY_ACCOUNT = {"password_hash": "0" * 64, "password_salt": "00" * 16}


class PostgresAccountStore:
    """Durable account registry backed by Postgres. Drop-in for AccountStore."""

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg  # noqa: F401
            from psycopg.rows import dict_row  # noqa: F401
        except ImportError as e:  # pragma: no cover - dep advertised in pyproject
            raise RuntimeError(
                "psycopg is required for the Postgres account store; "
                "install with `pip install 'psycopg[binary]>=3.1'`."
            ) from e
        # Normalise a SQLAlchemy-style DSN the other services may use.
        if dsn.startswith("postgresql+psycopg://"):
            dsn = "postgresql://" + dsn[len("postgresql+psycopg://") :]
        self._dsn = dsn
        with self._connect() as conn:
            for stmt in _SCHEMA_STATEMENTS:
                conn.execute(stmt)

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._connect() as conn:
            return conn.execute(sql, params).fetchone()

    # -- public surface (mirrors accounts.AccountStore) ---------------------

    def create(self, email: str, password: str, plan: str = "free") -> dict[str, Any]:
        import psycopg

        account = build_new_account(email, password, plan)
        values = [account[c] for c in ACCOUNT_COLUMNS]
        try:
            with self._connect() as conn:
                conn.execute(
                    f"INSERT INTO accounts ({_COLS}) VALUES ({_PLACEHOLDERS})", values
                )
        except psycopg.errors.UniqueViolation as e:
            # The email PK is the expected collision; the api_key UNIQUE
            # constraint can also fire (a ~192-bit key clash is astronomically
            # unlikely but not impossible) — don't mislabel that as a dup email.
            if "api_key" in (e.diag.constraint_name or ""):
                raise AccountError(
                    "could not allocate a unique API key; please retry"
                ) from None
            raise AccountError("an account with this email already exists") from None
        return dict(account)

    def verify_login(self, email: str, password: str) -> dict[str, Any] | None:
        email = email.strip().lower()
        account = self._one("SELECT * FROM accounts WHERE email = %s", (email,))
        if account is None:
            verify_password(_DUMMY_ACCOUNT, password)  # equal-time on unknown email
            return None
        return dict(account) if verify_password(account, password) else None

    def get(self, email: str) -> dict[str, Any] | None:
        a = self._one("SELECT * FROM accounts WHERE email = %s", (email.strip().lower(),))
        return dict(a) if a else None

    def resolve_key(self, api_key: str) -> dict[str, Any] | None:
        a = self._one(
            "SELECT tenant_id, plan, email FROM accounts WHERE api_key = %s", (api_key,)
        )
        if a is None:
            return None
        return {"tenant_id": a["tenant_id"], "tier": a["plan"], "email": a["email"]}

    def rotate_key(self, email: str) -> dict[str, Any] | None:
        a = self._one(
            "UPDATE accounts SET api_key = %s WHERE email = %s RETURNING *",
            (new_api_key(), email.strip().lower()),
        )
        return dict(a) if a else None

    def set_plan(self, email: str, plan: str) -> dict[str, Any] | None:
        if plan not in PLANS:
            raise AccountError(f"unknown plan {plan!r}")
        a = self._one(
            "UPDATE accounts SET plan = %s WHERE email = %s RETURNING *",
            (plan, email.strip().lower()),
        )
        return dict(a) if a else None

    def attach_stripe(
        self,
        email: str,
        customer_id: str | None = None,
        subscription_id: str | None = None,
    ) -> dict[str, Any] | None:
        # COALESCE leaves the existing value when an arg is None — matches
        # AccountStore, which only overwrites on a truthy value.
        a = self._one(
            "UPDATE accounts SET "
            "stripe_customer_id = COALESCE(%s, stripe_customer_id), "
            "stripe_subscription_id = COALESCE(%s, stripe_subscription_id) "
            "WHERE email = %s RETURNING *",
            (customer_id or None, subscription_id or None, email.strip().lower()),
        )
        return dict(a) if a else None

    def email_for_customer(self, customer_id: str) -> str | None:
        a = self._one(
            "SELECT email FROM accounts WHERE stripe_customer_id = %s", (customer_id,)
        )
        return a["email"] if a else None

    # -- email verification / password reset --------------------------------
    # Tokens are matched by exact hash + a server-side expiry check, both in
    # the WHERE clause, so consuming a token is a single atomic UPDATE.

    def issue_verification(self, email: str) -> str | None:
        token = new_email_token()
        a = self._one(
            "UPDATE accounts SET verify_token_hash = %s, verify_token_expires = %s "
            "WHERE email = %s RETURNING email",
            (_hash_token(token), time.time() + VERIFY_TOKEN_TTL_S, email.strip().lower()),
        )
        return token if a else None

    def verify_email(self, token: str) -> str | None:
        a = self._one(
            "UPDATE accounts SET verified = TRUE, verify_token_hash = NULL, "
            "verify_token_expires = NULL "
            "WHERE verify_token_hash = %s AND verify_token_expires >= %s RETURNING email",
            (_hash_token(token), time.time()),
        )
        return a["email"] if a else None

    def issue_password_reset(self, email: str) -> str | None:
        token = new_email_token()
        a = self._one(
            "UPDATE accounts SET reset_token_hash = %s, reset_token_expires = %s "
            "WHERE email = %s RETURNING email",
            (_hash_token(token), time.time() + RESET_TOKEN_TTL_S, email.strip().lower()),
        )
        return token if a else None

    def reset_password(self, token: str, new_password: str) -> str | None:
        fields = build_password_fields(new_password)  # validates length first
        a = self._one(
            "UPDATE accounts SET password_salt = %s, password_hash = %s, "
            "reset_token_hash = NULL, reset_token_expires = NULL "
            "WHERE reset_token_hash = %s AND reset_token_expires >= %s RETURNING email",
            (fields["password_salt"], fields["password_hash"], _hash_token(token), time.time()),
        )
        return a["email"] if a else None

    def public_view(self, account: dict[str, Any]) -> dict[str, Any]:
        return account_public_view(account)


__all__ = ["PostgresAccountStore"]
