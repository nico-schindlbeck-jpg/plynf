# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Internal HTTP wrapper around ``httpx`` with auth and error mapping.

This module is **not** part of the public API. It exists so that
``workspace.py``, ``tools.py``, and ``client.py`` can speak to the
backing services without each one re-implementing auth headers and the
``{"error": {...}}`` envelope unwrap.
"""

from __future__ import annotations

from typing import Any

import httpx

from .exceptions import (
    BranchNotFound,
    ChannelNotFound,
    CostCapExceeded,
    FileNotFound,
    InvalidArguments,
    InvalidToken,
    InvalidWorkflowStep,
    KeyNotFound,
    LeaseConflict,
    LeaseNotHeld,
    LockConflict,
    LockNotFound,
    LockNotHeld,
    MessageNotFound,
    PlinthError,
    RateLimited,
    SchemaViolation,
    SnapshotNotFound,
    TokenExpired,
    TokenRevoked,
    ToolInvocationError,
    ToolNotFound,
    TransactionInvalidStatus,
    TransactionNotFound,
    Unauthorized,
    WorkerNotFound,
    WorkflowNotFound,
    WorkflowStepNotFound,
    WorkspaceNotFound,
)

# Map error codes from the Plinth error envelope to typed exceptions.
# When a 404 arrives without an explicit code, the caller hints the
# expected resource via ``not_found_class`` on the request method.
_CODE_TO_EXCEPTION: dict[str, type[PlinthError]] = {
    "WORKSPACE_NOT_FOUND": WorkspaceNotFound,
    "KEY_NOT_FOUND": KeyNotFound,
    "FILE_NOT_FOUND": FileNotFound,
    "SNAPSHOT_NOT_FOUND": SnapshotNotFound,
    "BRANCH_NOT_FOUND": BranchNotFound,
    "TOOL_NOT_FOUND": ToolNotFound,
    "CHANNEL_NOT_FOUND": ChannelNotFound,
    "MESSAGE_NOT_FOUND": MessageNotFound,
    "WORKFLOW_NOT_FOUND": WorkflowNotFound,
    "WORKFLOW_STEP_NOT_FOUND": WorkflowStepNotFound,
    "INVALID_WORKFLOW_STEP": InvalidWorkflowStep,
    "TRANSACTION_NOT_FOUND": TransactionNotFound,
    "TRANSACTION_INVALID_STATUS": TransactionInvalidStatus,
    "TRANSACTION_RENDER_ERROR": InvalidArguments,
    "SCHEMA_VIOLATION": SchemaViolation,
    "INVALID_ARGUMENTS": InvalidArguments,
    "UNAUTHORIZED": Unauthorized,
    "INVALID_TOKEN": InvalidToken,
    "TOKEN_EXPIRED": TokenExpired,
    "TOKEN_REVOKED": TokenRevoked,
    "RATE_LIMITED": RateLimited,
    "COST_CAP_EXCEEDED": CostCapExceeded,
    "TOOL_INVOCATION_FAILED": ToolInvocationError,
    "LEASE_CONFLICT": LeaseConflict,
    "LEASE_NOT_HELD": LeaseNotHeld,
    "WORKER_NOT_FOUND": WorkerNotFound,
    # v0.6 — generic resource locks. The workspace service emits
    # ``LOCK_HELD`` on contention; the SDK exposes it as :class:`LockConflict`
    # so user code reads naturally (``except LockConflict:``). The
    # ``LOCK_CONFLICT`` alias is accepted for symmetry with future services
    # that may surface the spec's preferred code directly.
    "LOCK_HELD": LockConflict,
    "LOCK_CONFLICT": LockConflict,
    "LOCK_NOT_HELD": LockNotHeld,
    "LOCK_NOT_FOUND": LockNotFound,
}

_STATUS_TO_EXCEPTION: dict[int, type[PlinthError]] = {
    400: InvalidArguments,
    401: Unauthorized,
    429: RateLimited,
}


class HTTPClient:
    """Thin wrapper around ``httpx.Client`` that adds Plinth auth & errors.

    A single ``HTTPClient`` is bound to one base URL (workspace or
    gateway). The ``Plinth`` facade owns one wrapper per service.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "plinth-python-sdk/0.1.0",
        }
        # ``transport`` is wired by tests using ``respx.MockTransport``;
        # in production callers leave it ``None`` and httpx picks the
        # default async-capable transport.
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> HTTPClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Verb helpers
    # ------------------------------------------------------------------

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        not_found_class: type[PlinthError] | None = None,
    ) -> httpx.Response:
        """Issue a GET request and return the raw response."""
        response = self._client.get(path, params=_clean_params(params))
        self._raise_for_status(response, not_found_class=not_found_class)
        return response

    def post(
        self,
        path: str,
        *,
        json: Any | None = None,
        content: bytes | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        not_found_class: type[PlinthError] | None = None,
    ) -> httpx.Response:
        """Issue a POST request and return the raw response."""
        response = self._client.post(
            path,
            json=json,
            content=content,
            params=_clean_params(params),
            headers=headers,
        )
        self._raise_for_status(response, not_found_class=not_found_class)
        return response

    def put(
        self,
        path: str,
        *,
        json: Any | None = None,
        content: bytes | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        not_found_class: type[PlinthError] | None = None,
    ) -> httpx.Response:
        """Issue a PUT request and return the raw response."""
        response = self._client.put(
            path,
            json=json,
            content=content,
            params=_clean_params(params),
            headers=headers,
        )
        self._raise_for_status(response, not_found_class=not_found_class)
        return response

    def delete(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        not_found_class: type[PlinthError] | None = None,
    ) -> httpx.Response:
        """Issue a DELETE request and return the raw response."""
        response = self._client.delete(path, params=_clean_params(params))
        self._raise_for_status(response, not_found_class=not_found_class)
        return response

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        not_found_class: type[PlinthError] | None = None,
    ) -> Any:
        """Convenience: GET and return the JSON-decoded body."""
        return self.get(path, params=params, not_found_class=not_found_class).json()

    # ------------------------------------------------------------------
    # Error mapping
    # ------------------------------------------------------------------

    def _raise_for_status(
        self,
        response: httpx.Response,
        *,
        not_found_class: type[PlinthError] | None = None,
    ) -> None:
        """Map any non-2xx response to a typed Plinth exception."""
        if response.is_success:
            return

        envelope = _safe_json(response)
        error_obj = (envelope or {}).get("error", {}) if isinstance(envelope, dict) else {}
        code = error_obj.get("code")
        message = error_obj.get("message") or response.text or response.reason_phrase
        details = error_obj.get("details") or {}

        # 1. Prefer the explicit error code from the response envelope.
        exc_class: type[PlinthError] | None = _CODE_TO_EXCEPTION.get(code) if code else None

        # 2. Fall back to the status-code map.
        if exc_class is None:
            exc_class = _STATUS_TO_EXCEPTION.get(response.status_code)

        # 3. For 404s, prefer the resource-specific class hinted by the
        #    caller (e.g. ``KeyNotFound`` for the KV endpoints).
        if exc_class is None and response.status_code == 404:
            exc_class = not_found_class or PlinthError

        # 4. Final catch-all: 5xx and anything else.
        if exc_class is None:
            exc_class = PlinthError

        # ``RateLimited`` (and its ``CostCapExceeded`` subclass) carry
        # extra retry metadata pulled from the response. We compute it
        # here so callers can sleep/back-off without re-reading headers.
        if response.status_code == 429 or issubclass(exc_class, RateLimited):
            retry_after = _parse_retry_after(response, details)
            reason = details.get("limit_type", "") if isinstance(details, dict) else ""
            current = details.get("current") if isinstance(details, dict) else None
            limit = details.get("limit") if isinstance(details, dict) else None
            raise exc_class(
                message,
                code=code,
                details=details,
                response=response,
                retry_after=retry_after,
                reason=reason or "",
                current=current,
                limit=limit,
            )

        # ``LockConflict`` (v0.6) — surface ``current_holder`` and the
        # server's back-off hint directly so callers don't have to dig
        # through ``e.details``.
        if issubclass(exc_class, LockConflict):
            current_holder = (
                details.get("current_holder")
                if isinstance(details, dict)
                else None
            )
            retry_after_seconds = (
                details.get("retry_after_seconds")
                if isinstance(details, dict)
                else None
            )
            raise exc_class(
                message,
                code=code,
                details=details,
                response=response,
                current_holder=current_holder,
                retry_after_seconds=retry_after_seconds,
            )

        # ``SchemaViolation`` (v0.5) — surface the validator errors and the
        # DLQ message ID directly on the exception so callers don't need to
        # reach into ``e.details``.
        if issubclass(exc_class, SchemaViolation):
            errors = (
                details.get("errors")
                if isinstance(details, dict) and isinstance(details.get("errors"), list)
                else []
            )
            dlq = (
                details.get("deadletter_msg_id")
                if isinstance(details, dict)
                else None
            )
            channel = details.get("channel") if isinstance(details, dict) else None
            raise exc_class(
                message,
                code=code,
                details=details,
                response=response,
                errors=errors,
                deadletter_msg_id=dlq,
                channel=channel,
            )

        raise exc_class(message, code=code, details=details, response=response)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_json(response: httpx.Response) -> Any | None:
    """Best-effort JSON decode; returns ``None`` for non-JSON bodies."""
    try:
        return response.json()
    except (ValueError, UnicodeDecodeError):
        return None


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip ``None`` values so they don't end up as ``?key=None``."""
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _parse_retry_after(
    response: httpx.Response,
    details: dict[str, Any] | None,
) -> float:
    """Extract a ``retry_after`` (seconds) from a 429 response.

    Prefers the structured ``details.retry_after_seconds`` from the error
    envelope, then falls back to the ``Retry-After`` HTTP header. Returns
    ``0.0`` when neither is parsable.
    """
    if isinstance(details, dict):
        raw = details.get("retry_after_seconds")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass

    header = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


__all__ = ["HTTPClient"]
