# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""RS256 signing-key store + rotation logic for the identity service.

The identity service mints capability tokens. With ``jwt_alg=HS256`` it
signs them with a single shared secret; with ``jwt_alg=RS256`` it signs
them with a private RSA key whose corresponding public key is served via
``GET /v1/.well-known/jwks.json``.

This module owns:

* RSA-2048 key generation
* AES-GCM at-rest encryption of private PEMs
* The :class:`KeyStore` async API (init / active / rotate / list / expire /
  auto-rotate-if-due)
* Helpers to project a stored :class:`SigningKey` into the JWK shape that
  ``GET /v1/.well-known/jwks.json`` returns.

All operations against the underlying SQLite table go through
:func:`store.connect`, so the module composes cleanly with the rest of
the identity service's persistence layer.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .models import SigningKey
from .settings import Settings
from .store import connect

UTC = timezone.utc  # noqa: UP017
RSA_KEY_SIZE = 2048
PUBLIC_EXPONENT = 65537


# ---------------------------------------------------------------------------
# Key generation primitives


def generate_rsa_keypair() -> tuple[bytes, bytes]:
    """Generate a fresh RSA-2048 keypair.

    Returns:
        ``(private_pem, public_pem)`` as PEM-encoded bytes. The private
        key is PKCS#8 / unencrypted (encryption is the caller's concern —
        :func:`encrypt_private_key` does it with a fresh AES-GCM nonce).
    """

    key = rsa.generate_private_key(
        public_exponent=PUBLIC_EXPONENT,
        key_size=RSA_KEY_SIZE,
    )
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def kid_for(public_pem: bytes) -> str:
    """Derive a stable key id from the public PEM.

    Returns the first 16 hex chars of ``sha256(public_pem)``. Stable means
    "for a given public PEM you always get the same kid". Two different
    keypairs will (overwhelmingly likely) produce different kids.
    """

    return hashlib.sha256(public_pem).hexdigest()[:16]


# ---------------------------------------------------------------------------
# AES-GCM at-rest wrapping
#
# We wrap the private PEM with a 32-byte AES-GCM key from settings. The
# ciphertext is stored as ``base64(nonce || ciphertext)`` so the column
# stays ASCII and round-trips cleanly through SQLite TEXT.


def encrypt_private_key(private_pem: bytes, encryption_key: bytes) -> str:
    """Wrap ``private_pem`` with AES-GCM.

    Args:
        private_pem: Raw PEM bytes (PKCS#8, no password).
        encryption_key: 32-byte AES-256-GCM key.

    Returns:
        Base64 string ``nonce || ciphertext`` suitable for storage.
    """

    if len(encryption_key) != 32:
        raise ValueError("AES-GCM encryption key must be 32 bytes")
    aes = AESGCM(encryption_key)
    # 96-bit nonces are the AES-GCM sweet spot. Random nonces are safe
    # because we never re-encrypt the same plaintext under the same key
    # twice (each rotation generates fresh PEMs).
    import os

    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, private_pem, associated_data=None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_private_key(encrypted: str, encryption_key: bytes) -> bytes:
    """Inverse of :func:`encrypt_private_key`."""

    if len(encryption_key) != 32:
        raise ValueError("AES-GCM encryption key must be 32 bytes")
    blob = base64.b64decode(encrypted)
    if len(blob) < 13:
        raise ValueError("encrypted blob is too short to contain a nonce")
    nonce, ct = blob[:12], blob[12:]
    aes = AESGCM(encryption_key)
    return aes.decrypt(nonce, ct, associated_data=None)


# ---------------------------------------------------------------------------
# JWK projection (for the JWKS endpoint)


def _b64url_uint(n: int) -> str:
    """Base64url-encode a positive integer per RFC 7518 §6.3.1.1."""

    byte_len = (n.bit_length() + 7) // 8 or 1
    raw = n.to_bytes(byte_len, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def public_pem_to_jwk(public_pem: bytes, kid: str, alg: str = "RS256") -> dict[str, Any]:
    """Return the JWK representation of ``public_pem``.

    Used both server-side to render ``/v1/.well-known/jwks.json`` and
    client-side to reconstruct a usable PEM for PyJWT.
    """

    public_key = serialization.load_pem_public_key(public_pem)
    if not isinstance(public_key, RSAPublicKey):
        raise ValueError("expected an RSA public key")
    numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "alg": alg,
        "use": "sig",
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }


def jwk_to_pem(jwk: dict[str, Any]) -> bytes:
    """Reconstruct a public PEM from its JWK projection.

    Symmetric with :func:`public_pem_to_jwk`. Verifiers in workspace +
    gateway use this to feed PyJWT a key it understands.
    """

    n_b64 = jwk.get("n")
    e_b64 = jwk.get("e")
    if not n_b64 or not e_b64:
        raise ValueError("JWK is missing 'n' or 'e'")
    n = int.from_bytes(_b64url_decode(n_b64), "big")
    e = int.from_bytes(_b64url_decode(e_b64), "big")
    public_key = rsa.RSAPublicNumbers(e=e, n=n).public_key()
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _b64url_decode(value: str) -> bytes:
    """Decode a base64url string regardless of whether it carries padding."""

    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded)


# ---------------------------------------------------------------------------
# Schema + persistence

SIGNING_KEYS_SCHEMA = """
CREATE TABLE IF NOT EXISTS signing_keys (
  kid TEXT PRIMARY KEY,
  alg TEXT NOT NULL,
  public_key_pem TEXT NOT NULL,
  private_key_pem_encrypted TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  rotated_in_at TIMESTAMP,
  expires_at TIMESTAMP NOT NULL,
  active INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_keys_active ON signing_keys(active, expires_at);
"""


async def init_keys_schema(db_path: Path) -> None:
    """Apply the ``signing_keys`` schema idempotently.

    Called by :func:`store.init_db` indirectly via :class:`KeyStore.init`,
    or directly when an admin wants to provision a brand-new identity DB.
    """

    async with connect(db_path) as conn:
        await conn.executescript(SIGNING_KEYS_SCHEMA)
        await conn.commit()


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.isoformat()


def _parse_ts(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _row_to_signing_key(row: aiosqlite.Row) -> SigningKey:
    created_at = _parse_ts(row["created_at"])
    expires_at = _parse_ts(row["expires_at"])
    rotated_in_at = _parse_ts(row["rotated_in_at"])
    assert created_at is not None and expires_at is not None  # noqa: S101
    return SigningKey(
        kid=row["kid"],
        alg=row["alg"],
        public_key_pem=row["public_key_pem"],
        created_at=created_at,
        rotated_in_at=rotated_in_at,
        expires_at=expires_at,
        active=bool(row["active"]),
    )


# ---------------------------------------------------------------------------
# KeyStore


class KeyStore:
    """Manages RS256 signing keys with rotation.

    Public API mirrors the one in the spec docstring of CONTRACTS.md →
    "RS256 Capability Tokens (Identity)".

    The store is async because it sits on top of :mod:`aiosqlite`. The
    per-call connection cost is low and matches the rest of the identity
    service's storage layer.

    Attributes:
        rotation_days: How long a freshly minted active key stays current
            before :meth:`auto_rotate_if_due` rotates it out. The retired
            key remains *valid for verification* until its
            ``expires_at`` (which is set to twice the rotation window so
            outstanding tokens still verify).
    """

    def __init__(self, db_path: Path, settings: Settings) -> None:
        self._db_path = db_path
        self._settings = settings
        self._encryption_key: bytes | None = None

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def rotation_days(self) -> int:
        return self._settings.identity_key_rotation_days

    @property
    def jwks_max_keys(self) -> int:
        return self._settings.identity_jwks_max_keys

    def _enc_key(self) -> bytes:
        # Resolve once per process — settings are immutable for the
        # lifetime of the app and hitting disk on every encrypt/decrypt
        # would be wasteful.
        if self._encryption_key is None:
            self._encryption_key = self._settings.resolve_keys_encryption_key()
        return self._encryption_key

    # -------------------------------------------------------------- lifecycle

    async def init(self) -> None:
        """Ensure the schema exists and at least one active key is present.

        - Always applies the schema (idempotent).
        - Generates a first key only when ``jwt_alg=RS256``.
        - If the latest active key is older than ``rotation_days``, rotates.
        """

        await init_keys_schema(self._db_path)

        if self._settings.identity_jwt_alg != "RS256":
            return

        keys = await self.list_keys(include_expired=False)
        active = [k for k in keys if k.active]
        if not active:
            await self._generate_initial_key()
            return

        await self.auto_rotate_if_due()

    async def _generate_initial_key(self) -> SigningKey:
        return await self._generate_and_store(active=True, rotated_in=True)

    async def _generate_and_store(
        self,
        *,
        active: bool,
        rotated_in: bool,
        now: datetime | None = None,
    ) -> SigningKey:
        """Generate, encrypt, and persist a fresh keypair."""

        private_pem, public_pem = generate_rsa_keypair()
        kid = kid_for(public_pem)
        encrypted = encrypt_private_key(private_pem, self._enc_key())
        # Keep microseconds on created_at so two rotations issued back-to-back
        # don't tie in ORDER BY clauses. expires_at can stay second-rounded.
        ts = now or datetime.now(UTC)
        # Verifiers must be able to validate tokens issued under this key
        # for the full lifetime of any token that could have been minted
        # before rotation. ``rotation_days * 2`` gives a comfortable buffer
        # for a max-TTL token (24h) issued moments before rotation.
        expires_at = ts + timedelta(days=self.rotation_days * 2)

        async with connect(self._db_path) as conn:
            if active:
                # Demote any other active keys atomically — only one
                # ``active=1`` row exists at a time.
                await conn.execute("UPDATE signing_keys SET active = 0")
            await conn.execute(
                "INSERT INTO signing_keys "
                "(kid, alg, public_key_pem, private_key_pem_encrypted, "
                " created_at, rotated_in_at, expires_at, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kid,
                    "RS256",
                    public_pem.decode("ascii"),
                    encrypted,
                    _iso(ts),
                    _iso(ts) if rotated_in else None,
                    _iso(expires_at),
                    1 if active else 0,
                ),
            )
            await conn.commit()

        return SigningKey(
            kid=kid,
            alg="RS256",
            public_key_pem=public_pem.decode("ascii"),
            created_at=ts,
            rotated_in_at=ts if rotated_in else None,
            expires_at=expires_at,
            active=active,
        )

    # -------------------------------------------------------------- queries

    async def active_key(self) -> SigningKey:
        """Return the currently active signing key.

        Raises:
            RuntimeError: when no active key exists. Callers should run
                :meth:`init` on startup so this never happens in a healthy
                deployment.
        """

        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM signing_keys "
                "WHERE active = 1 AND expires_at > ? "
                "ORDER BY created_at DESC LIMIT 1",
                (_iso(datetime.now(UTC)),),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            raise RuntimeError(
                "no active RS256 signing key; call KeyStore.init() first"
            )
        return _row_to_signing_key(row)

    async def get_by_kid(self, kid: str) -> SigningKey | None:
        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM signing_keys WHERE kid = ?",
                (kid,),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            return None
        return _row_to_signing_key(row)

    async def get_private_pem(self, kid: str) -> bytes:
        """Decrypt and return the private PEM for ``kid``.

        Used by :class:`TokenManager` only — the API never exposes private
        material.
        """

        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT private_key_pem_encrypted FROM signing_keys WHERE kid = ?",
                (kid,),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            raise KeyError(kid)
        return decrypt_private_key(row[0], self._enc_key())

    async def list_keys(
        self,
        *,
        include_expired: bool = False,
    ) -> list[SigningKey]:
        """Return all keys (newest first), optionally including expired ones."""

        sql = "SELECT * FROM signing_keys"
        params: tuple[Any, ...] = ()
        if not include_expired:
            sql += " WHERE expires_at > ?"
            params = (_iso(datetime.now(UTC)),)
        sql += " ORDER BY created_at DESC"

        async with connect(self._db_path) as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_signing_key(r) for r in rows]

    async def list_jwks_keys(self) -> list[SigningKey]:
        """Return up to ``jwks_max_keys`` non-expired keys for the JWKS doc."""

        keys = await self.list_keys(include_expired=False)
        return keys[: self.jwks_max_keys]

    # -------------------------------------------------------------- mutations

    async def rotate(self) -> SigningKey:
        """Generate a new active key. Demotes any prior active key.

        The retired key's row stays in the table — its public PEM keeps
        verifying tokens issued before rotation until its
        ``expires_at`` passes.
        """

        return await self._generate_and_store(active=True, rotated_in=True)

    async def auto_rotate_if_due(self) -> SigningKey | None:
        """Rotate if the active key was minted more than ``rotation_days`` ago.

        Returns the new key on rotation or ``None`` if rotation wasn't due.
        Idempotent: hammer the call as often as you like.
        """

        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM signing_keys WHERE active = 1 "
                "ORDER BY created_at DESC LIMIT 1"
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            # No active key at all → caller should have invoked init().
            return None
        active = _row_to_signing_key(row)
        rotation_threshold = datetime.now(UTC) - timedelta(days=self.rotation_days)
        if active.created_at <= rotation_threshold:
            return await self.rotate()
        return None

    async def expire(self, kid: str) -> None:
        """Force-expire a key.

        Sets ``expires_at`` to "now" and ``active=0``. Tokens signed by
        this key will fail signature verification (because the verifier's
        cached JWKS won't include it after the next refresh).
        """

        existing = await self.get_by_kid(kid)
        if existing is None:
            raise KeyError(kid)
        now = datetime.now(UTC).replace(microsecond=0)
        async with connect(self._db_path) as conn:
            await conn.execute(
                "UPDATE signing_keys SET active = 0, expires_at = ? WHERE kid = ?",
                (_iso(now), kid),
            )
            await conn.commit()

    # -------------------------------------------------------------- utilities

    def to_jwk(self, key: SigningKey) -> dict[str, Any]:
        """Project a :class:`SigningKey` into the JWKS dict shape."""

        return public_pem_to_jwk(
            key.public_key_pem.encode("ascii"),
            kid=key.kid,
            alg=key.alg,
        )


__all__ = [
    "KeyStore",
    "RSA_KEY_SIZE",
    "SIGNING_KEYS_SCHEMA",
    "decrypt_private_key",
    "encrypt_private_key",
    "generate_rsa_keypair",
    "init_keys_schema",
    "jwk_to_pem",
    "kid_for",
    "public_pem_to_jwk",
]
