# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Pydantic models for the identity service.

Mirrors ``CONTRACTS.md → Identity Service`` 1:1.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional  # noqa: UP035

from pydantic import BaseModel, ConfigDict, Field


class TokenIssueRequest(BaseModel):
    """Body of ``POST /v1/tokens``."""

    model_config = ConfigDict(extra="ignore")

    agent_id: str
    tenant_id: str = "default"
    scopes: List[str] = Field(default_factory=list)  # noqa: UP006
    workspace_id: Optional[str] = None  # noqa: UP045
    ttl_seconds: int = 3600
    metadata: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006
    rate_limit: Optional[Dict[str, Any]] = None  # noqa: UP006, UP045


class TokenClaims(BaseModel):
    """Claims embedded in (and recovered from) every JWT.

    The standard registered claims ``sub``, ``iss``, ``aud``, ``iat``, ``exp``,
    ``jti`` are required. Custom Plinth claims sit alongside them for ergonomic
    access by services.
    """

    model_config = ConfigDict(extra="ignore")

    sub: str
    iss: str
    aud: str
    iat: int
    exp: int
    jti: str
    agent_id: str
    tenant_id: str
    workspace_id: Optional[str] = None  # noqa: UP045
    scopes: List[str] = Field(default_factory=list)  # noqa: UP006
    rate_limit: Optional[Dict[str, Any]] = None  # noqa: UP006, UP045


class TokenIssueResponse(BaseModel):
    """Response from ``POST /v1/tokens``."""

    model_config = ConfigDict(extra="ignore")

    token: str
    jti: str
    expires_at: datetime
    claims: TokenClaims


class TokenInfo(BaseModel):
    """Public introspection view — never carries the JWT itself."""

    model_config = ConfigDict(extra="ignore")

    jti: str
    agent_id: str
    tenant_id: str
    workspace_id: Optional[str] = None  # noqa: UP045
    scopes: List[str] = Field(default_factory=list)  # noqa: UP006
    issued_at: datetime
    expires_at: datetime
    revoked: bool = False
    revoked_at: Optional[datetime] = None  # noqa: UP045
    metadata: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006


class TokenVerifyRequest(BaseModel):
    """Body of ``POST /v1/tokens/verify``."""

    model_config = ConfigDict(extra="ignore")

    token: str


class HealthResponse(BaseModel):
    """``GET /healthz`` payload."""

    model_config = ConfigDict(extra="ignore")

    status: str
    version: str
    service: str


class JWKSResponse(BaseModel):
    """``GET /v1/.well-known/jwks.json`` payload.

    For HS256 (shared secret), the keys list is empty by design — the secret is
    private to issuer + verifiers. We still expose the endpoint so downstream
    callers can discover the algorithm and migrate to RS256 cleanly.
    """

    model_config = ConfigDict(extra="ignore")

    keys: List[Dict[str, Any]] = Field(default_factory=list)  # noqa: UP006


class Tenant(BaseModel):
    """A tenant — the top-level isolation boundary across all Plinth services."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    metadata: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006
    created_at: datetime


class TenantCreate(BaseModel):
    """Body of ``POST /v1/tenants``."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    metadata: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006


class TenantList(BaseModel):
    """Response from ``GET /v1/tenants``."""

    model_config = ConfigDict(extra="ignore")

    tenants: List[Tenant] = Field(default_factory=list)  # noqa: UP006


class TokenInfoList(BaseModel):
    """Response from ``GET /v1/tokens``."""

    model_config = ConfigDict(extra="ignore")

    tokens: List[TokenInfo] = Field(default_factory=list)  # noqa: UP006


class SigningKey(BaseModel):
    """Public-safe view of an RS256 signing key.

    Exposed via ``GET /v1/keys`` and the rotate / expire admin endpoints.
    Never carries private key material.
    """

    model_config = ConfigDict(extra="ignore")

    kid: str
    alg: str
    public_key_pem: str
    created_at: datetime
    rotated_in_at: Optional[datetime] = None  # noqa: UP045
    expires_at: datetime
    active: bool = False


class SigningKeyList(BaseModel):
    """Response from ``GET /v1/keys``."""

    model_config = ConfigDict(extra="ignore")

    keys: List[SigningKey] = Field(default_factory=list)  # noqa: UP006


__all__ = [
    "HealthResponse",
    "JWKSResponse",
    "SigningKey",
    "SigningKeyList",
    "Tenant",
    "TenantCreate",
    "TenantList",
    "TokenClaims",
    "TokenInfo",
    "TokenInfoList",
    "TokenIssueRequest",
    "TokenIssueResponse",
    "TokenVerifyRequest",
]
