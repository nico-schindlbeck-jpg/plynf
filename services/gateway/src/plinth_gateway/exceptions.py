# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Typed exceptions for the gateway service.

Each exception carries an error ``code`` and ``http_status`` so the FastAPI
exception handler can map it to the wire format documented in ``CONTRACTS.md``.
"""

from __future__ import annotations

from typing import Any


class GatewayError(Exception):
    """Base exception for all gateway errors."""

    code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status


class ToolNotFound(GatewayError):
    """Raised when the requested tool is not registered."""

    code = "TOOL_NOT_FOUND"
    http_status = 404


class ToolAlreadyExists(GatewayError):
    """Raised on duplicate tool registration."""

    code = "INVALID_ARGUMENTS"
    http_status = 400


class ToolInvocationError(GatewayError):
    """Raised when a backend invocation fails (non-2xx, network, parse)."""

    code = "TOOL_INVOCATION_FAILED"
    http_status = 502


class TransportNotSupported(GatewayError):
    """Raised when a tool's transport isn't implemented in this version."""

    code = "TRANSPORT_NOT_SUPPORTED"
    http_status = 501


class InvalidArguments(GatewayError):
    """Raised on bad request payloads."""

    code = "INVALID_ARGUMENTS"
    http_status = 400


class Unauthorized(GatewayError):
    """Raised when inbound auth fails."""

    code = "UNAUTHORIZED"
    http_status = 401


class RateLimited(GatewayError):
    """Raised when the per-agent rate limit (requests-per-minute) is exceeded.

    Maps to HTTP 429 with a ``Retry-After`` header. The caller should wait at
    least ``retry_after`` seconds before retrying.
    """

    code = "RATE_LIMITED"
    http_status = 429

    def __init__(
        self,
        reason: str,
        retry_after: float,
        *,
        current: float | None = None,
        limit: float | None = None,
        message: str | None = None,
    ) -> None:
        self.reason = reason
        self.retry_after = float(retry_after)
        self.current = current
        self.limit = limit
        msg = (
            message
            if message is not None
            else f"Rate limit exceeded ({reason}). Retry after {self.retry_after:.0f}s."
        )
        details: dict[str, Any] = {
            "limit_type": reason,
            "retry_after_seconds": int(self.retry_after) if self.retry_after >= 1 else self.retry_after,
        }
        if current is not None:
            details["current"] = current
        if limit is not None:
            details["limit"] = limit
        super().__init__(msg, details=details)


class OAuthProviderNotConfigured(GatewayError):
    """Raised when an OAuth provider's client credentials are not configured.

    Maps to HTTP 503 — the feature isn't a runtime failure, it's an op-time
    misconfiguration. The error envelope explains how to set the env vars.
    """

    code = "OAUTH_NOT_CONFIGURED"
    http_status = 503


class OAuthError(GatewayError):
    """Raised on protocol-level OAuth failures (bad state, exchange failed)."""

    code = "OAUTH_ERROR"
    http_status = 400


class OAuthConnectionNotFound(GatewayError):
    """Raised when a referenced OAuth connection does not exist."""

    code = "OAUTH_CONNECTION_NOT_FOUND"
    http_status = 404


class TransactionNotFound(GatewayError):
    """Raised when the requested transaction does not exist."""

    code = "TRANSACTION_NOT_FOUND"
    http_status = 404


class TransactionInvalidStatus(GatewayError):
    """Raised when the transaction's lifecycle state forbids the requested op.

    Examples:
      * Adding a call to an already-committed transaction.
      * Committing a transaction that is not in ``pending`` status.
      * Rolling back a fully committed transaction (use a separate undo
        transaction instead).
    """

    code = "TRANSACTION_INVALID_STATUS"
    http_status = 409


class TransactionRenderError(GatewayError):
    """Raised when an argument template cannot be rendered.

    Templates reference ``{seq.<n>.result.<field>}`` or ``{result.<field>}``
    placeholders. A missing prior call, a missing field, or a malformed
    placeholder all raise this error to prevent silent data corruption.
    """

    code = "TRANSACTION_RENDER_ERROR"
    http_status = 400


class CostCapExceeded(GatewayError):
    """Raised when the rolling-window cost cap (1h or 24h) would be exceeded.

    Returns 429 like rate limiting but with a different ``code`` so callers can
    distinguish ‘slow down’ from ‘you spent too much’.
    """

    code = "COST_CAP_EXCEEDED"
    http_status = 429

    def __init__(
        self,
        reason: str,
        used: float,
        cap: float,
        *,
        retry_after: float = 60.0,
        message: str | None = None,
    ) -> None:
        self.reason = reason
        self.used = float(used)
        self.cap = float(cap)
        self.retry_after = float(retry_after)
        msg = (
            message
            if message is not None
            else f"Cost cap exceeded ({reason}). Used ${used:.4f} of ${cap:.4f}."
        )
        details: dict[str, Any] = {
            "limit_type": reason,
            "retry_after_seconds": int(self.retry_after) if self.retry_after >= 1 else self.retry_after,
            "current": self.used,
            "limit": self.cap,
        }
        super().__init__(msg, details=details)
