# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Runtime configuration for the identity service.

All values are read from environment variables prefixed with ``PLINTH_``.
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path
from typing import Literal, Optional  # noqa: UP035

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Identity-service settings.

    Attributes:
        data_dir: Root directory for the SQLite DB + secret material.
        identity_port: Port the FastAPI app listens on.
        identity_host: Host the FastAPI app binds to.
        identity_url: Public URL used as the JWT ``iss`` claim.
        log_level: Standard ``logging`` level name.
        log_format: ``console`` (dev) or ``json`` (prod) formatter.
        jwt_secret: Shared HS256 secret. Read from
            ``PLINTH_IDENTITY_JWT_SECRET`` if set; otherwise auto-generated
            and persisted to ``data_dir/identity-jwt-secret``.
        jwt_audience: ``aud`` claim value embedded in every issued token.
        jwt_default_ttl_seconds: Fallback TTL when issuer omits one.
        auto_generate_secret: When True, a missing secret is created on
            first use. Disable in production.
        identity_jwt_alg: Signing algorithm. ``HS256`` (default) keeps
            v0.3 back-compat; ``RS256`` enables key rotation + JWKS.
        identity_key_rotation_days: How long an active RS256 key stays
            "current" before auto-rotation generates a new one.
        identity_keys_dir: Filesystem location for RS256 key material
            (only used as the seed for ``KeyStore`` if you want to
            export keys; canonical storage lives in SQLite).
        identity_keys_encryption_key: Base64-encoded 32-byte AES-GCM key
            wrapping the private PEMs at rest. Auto-generated in dev with
            a WARNING log if missing.
    """

    model_config = SettingsConfigDict(
        env_prefix="PLINTH_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("/tmp/plinth-data"))
    identity_port: int = Field(default=7425)
    identity_host: str = Field(default="0.0.0.0")
    identity_url: str = Field(default="http://localhost:7425")
    log_level: str = Field(default="INFO")
    log_format: Literal["console", "json"] = Field(default="console")

    # JWT settings — picked up via PLINTH_IDENTITY_JWT_* aliases below.
    identity_jwt_secret: Optional[str] = Field(default=None)  # noqa: UP007, UP045
    identity_jwt_audience: str = Field(default="plinth")
    identity_jwt_default_ttl_seconds: int = Field(default=3600)
    # Max TTL the issuer will mint. 86400 = 24h matches the v0.3 spec.
    # Tokens beyond this are rejected at issue time so a misconfigured caller
    # can't accidentally mint a year-long token.
    identity_jwt_max_ttl_seconds: int = Field(default=86400)
    identity_auto_generate_secret: bool = Field(default=True)

    # v0.4 — RS256 + key rotation (additive; HS256 stays the default).
    identity_jwt_alg: Literal["HS256", "RS256"] = Field(default="HS256")
    identity_key_rotation_days: int = Field(default=30)
    identity_keys_dir: Optional[Path] = Field(default=None)  # noqa: UP007, UP045
    identity_keys_encryption_key: str = Field(default="")
    # Cap the JWKS response so a long history of rotated keys doesn't make
    # the document grow unbounded. The spec says "last 3 non-expired".
    identity_jwks_max_keys: int = Field(default=3)

    # v0.4 — pluggable storage driver. ``sqlite`` is the default.
    storage_driver: Literal["sqlite", "postgres"] = Field(default="sqlite")
    database_url: str = Field(default="")
    identity_database_url: str = Field(default="")
    db_pool_min_size: int = Field(default=5)
    db_pool_max_size: int = Field(default=20)

    # v0.5 — schema migration framework. Default True applies pending
    # migrations on startup. Set False where migration application is
    # gated by an operator (CI/CD pipeline, blue/green deploy). When False
    # the service still starts but emits a WARNING per pending migration.
    auto_migrate: bool = Field(default=True)

    @property
    def effective_database_url(self) -> str:
        """Service-specific URL wins, then the shared one."""

        return self.identity_database_url or self.database_url

    @property
    def db_path(self) -> Path:
        """Absolute path to the SQLite database file."""
        return self.data_dir / "identity.db"

    @property
    def secret_path(self) -> Path:
        """Where an auto-generated HS256 secret is persisted."""
        return self.data_dir / "identity-jwt-secret"

    @property
    def keys_dir_path(self) -> Path:
        """Resolved keys directory (defaults under ``data_dir``)."""

        return self.identity_keys_dir or (self.data_dir / "identity-keys")

    @property
    def keys_encryption_key_path(self) -> Path:
        """Where an auto-generated AES-GCM key is persisted."""

        return self.data_dir / "identity-keys-encryption-key"

    def resolve_secret(self) -> str:
        """Return the HS256 secret, generating + persisting one if missing.

        Resolution order:

        1. ``PLINTH_IDENTITY_JWT_SECRET`` env var (already on ``self``).
        2. Cached file at ``self.secret_path``.
        3. Generate a fresh 32-byte secret (only when
           ``identity_auto_generate_secret`` is True).

        Raises:
            RuntimeError: when no secret is configured and auto-generation
                is disabled.
        """

        if self.identity_jwt_secret:
            return self.identity_jwt_secret

        path = self.secret_path
        if path.exists():
            return path.read_text(encoding="utf-8").strip()

        if not self.identity_auto_generate_secret:
            raise RuntimeError(
                "PLINTH_IDENTITY_JWT_SECRET is not set and auto-generation is "
                "disabled. Set the env var to a 32+ byte base64 string."
            )

        # 32 bytes is RFC 7518 §3.2's recommended minimum for HS256. We base64
        # the bytes so the on-disk file is line-safe and copy-pasteable.
        secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Best-effort tighten perms; on platforms without chmod (e.g. some
        # CI runners) we silently fall through.
        path.write_text(secret, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return secret

    def resolve_keys_encryption_key(self) -> bytes:
        """Return the 32-byte AES-GCM key used to wrap private RSA PEMs.

        Resolution order:

        1. ``PLINTH_IDENTITY_KEYS_ENCRYPTION_KEY`` env var (base64).
        2. Cached file at ``self.keys_encryption_key_path``.
        3. Auto-generate a fresh 32-byte key (only when
           ``identity_auto_generate_secret`` is True) and persist it.

        Raises:
            RuntimeError: when no key is configured and auto-generation
                is disabled.
            ValueError: when the supplied key isn't valid 32-byte base64.
        """

        if self.identity_keys_encryption_key:
            raw = base64.b64decode(self.identity_keys_encryption_key)
            if len(raw) != 32:
                raise ValueError(
                    "PLINTH_IDENTITY_KEYS_ENCRYPTION_KEY must decode to 32 bytes"
                )
            return raw

        path = self.keys_encryption_key_path
        if path.exists():
            raw = base64.b64decode(path.read_text(encoding="utf-8").strip())
            if len(raw) != 32:
                raise ValueError(
                    f"persisted encryption key at {path} is not 32 bytes"
                )
            return raw

        if not self.identity_auto_generate_secret:
            raise RuntimeError(
                "PLINTH_IDENTITY_KEYS_ENCRYPTION_KEY is not set and "
                "auto-generation is disabled. Set the env var to a base64 "
                "32-byte AES key."
            )

        raw = secrets.token_bytes(32)
        encoded = base64.b64encode(raw).decode("ascii")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return raw


def get_settings(**overrides: object) -> Settings:
    """Construct a fresh :class:`Settings`.

    Tests can pass overrides directly to bypass env vars without touching
    the global state.
    """

    return Settings(**overrides)  # type: ignore[arg-type]
