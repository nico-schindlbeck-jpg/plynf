# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Internal HTTP wrapper around ``httpx`` with auth and error mapping.

This module is **not** part of the public API. It exists so that
``workspace.py``, ``tools.py``, and ``client.py`` can speak to the
backing services without each one re-implementing auth headers and the
``{"error": {...}}`` envelope unwrap.
"""

from __future__ import annotations

import logging
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


_logger = logging.getLogger("plinth.sdk.http")


class HTTPClient:
    """Thin wrapper around ``httpx.Client`` that adds Plinth auth & errors.

    A single ``HTTPClient`` is bound to one primary base URL (workspace or
    gateway). The ``Plinth`` facade owns one wrapper per service.

    v1.0 — multi-region. Pass ``fallback_urls={region_id: url, ...}`` and
    the client will try fallbacks in iteration order on:

    * connection errors (``httpx.ConnectError``, ``httpx.ConnectTimeout``,
      ``httpx.ReadTimeout``)
    * 5xx responses (the primary is up but degraded)
    * 503 responses
    * 421 / 409 responses carrying an ``X-Plinth-Primary-Region`` and/or
      ``X-Plinth-Primary-URL`` header — when the named region exists in
      ``fallback_urls`` (or the URL header points at a known peer) we
      retry there directly. This is the read-replica redirect path.
      ``X-Plinth-Primary-URL`` wins over the region lookup when both are
      present, so a replica with no peer wired into the SDK can still
      hand back the canonical primary location.

    The redirect retry is **bounded**: each unique base URL is tried at
    most once per request (tracked in an ``attempted`` set) so a
    misconfigured pair of replicas can never produce an infinite loop.

    Fallback ordering is *deterministic*: ``fallback_urls`` is a dict so
    iteration order matches insertion order (Python 3.7+ guarantee).
    Tests that rely on it pass a regular ``dict`` and get the same
    behaviour the operator would in production.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        *,
        transport: httpx.BaseTransport | None = None,
        fallback_urls: dict[str, str] | None = None,
        primary_region: str | None = None,
    ) -> None:
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "plinth-python-sdk/0.1.0",
        }
        self._timeout = timeout
        self._transport = transport
        # The primary URL is always tried first.
        self._primary_url = base_url.rstrip("/")
        self._primary_region = primary_region
        # Deterministic ordering; dict preserves insertion order.
        self._fallback_urls: dict[str, str] = {
            region: url.rstrip("/")
            for region, url in (fallback_urls or {}).items()
        }
        # ``transport`` is wired by tests using ``respx.MockTransport``;
        # in production callers leave it ``None`` and httpx picks the
        # default async-capable transport.
        self._client = httpx.Client(
            base_url=self._primary_url,
            headers=self._headers,
            timeout=timeout,
            transport=transport,
        )
        # Per-fallback client, lazily built on first use.
        self._fallback_clients: dict[str, httpx.Client] = {}
        # Track which URL is the current primary. Failover updates this
        # so subsequent requests don't re-try the dead primary first.
        self._current_url = self._primary_url

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()
        for client in self._fallback_clients.values():
            try:
                client.close()
            except Exception:  # pragma: no cover - best-effort
                pass

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
        return self._request(
            "GET",
            path,
            params=params,
            not_found_class=not_found_class,
        )

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
        return self._request(
            "POST",
            path,
            json=json,
            content=content,
            params=params,
            extra_headers=headers,
            not_found_class=not_found_class,
        )

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
        return self._request(
            "PUT",
            path,
            json=json,
            content=content,
            params=params,
            extra_headers=headers,
            not_found_class=not_found_class,
        )

    def delete(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        not_found_class: type[PlinthError] | None = None,
    ) -> httpx.Response:
        """Issue a DELETE request and return the raw response."""
        return self._request(
            "DELETE",
            path,
            params=params,
            not_found_class=not_found_class,
        )

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
    # Multi-region failover
    # ------------------------------------------------------------------

    def _client_for(self, base_url: str) -> httpx.Client:
        """Return (or lazily build) an ``httpx.Client`` bound to ``base_url``."""

        if base_url == self._primary_url:
            return self._client
        existing = self._fallback_clients.get(base_url)
        if existing is not None:
            return existing
        new_client = httpx.Client(
            base_url=base_url,
            headers=self._headers,
            timeout=self._timeout,
            transport=self._transport,
        )
        self._fallback_clients[base_url] = new_client
        return new_client

    def _candidates_in_order(self) -> list[tuple[str, str]]:
        """Return ``[(region_id, url), ...]`` to try, in priority order.

        The primary always goes first (under its declared region id when
        ``primary_region`` is configured, else as ``"<primary>"``). The
        rest are the fallbacks in insertion order.
        """

        seq: list[tuple[str, str]] = [(self._primary_region or "<primary>", self._primary_url)]
        for region, url in self._fallback_urls.items():
            seq.append((region, url))
        return seq

    def _resolve_redirect_target(self, response: httpx.Response) -> str | None:
        """Pick the redirect URL from ``X-Plinth-Primary-*`` headers.

        Resolution order:

        1. ``X-Plinth-Primary-URL`` — explicit URL the server told us to
           use. We trust it iff its origin already appears in our known
           candidate set (primary URL or any configured fallback URL).
           Trusting a header URL blindly would let a malicious response
           steer the SDK at an attacker-controlled host.
        2. ``X-Plinth-Primary-Region`` looked up against ``fallback_urls``.
        3. ``X-Plinth-Primary-Region`` matching ``primary_region`` →
           the configured primary URL itself.

        Returns ``None`` when no trusted target can be derived.
        """

        url_hint = (response.headers.get("X-Plinth-Primary-URL") or "").strip()
        if url_hint:
            normalized = url_hint.rstrip("/")
            known = {self._primary_url, *self._fallback_urls.values()}
            if normalized in known:
                return normalized

        target_region = (
            response.headers.get("X-Plinth-Primary-Region") or ""
        ).strip()
        if not target_region:
            return None
        target_url = self._fallback_urls.get(target_region)
        if target_url is not None:
            return target_url
        if self._primary_region == target_region:
            return self._primary_url
        return None

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        content: bytes | None = None,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        not_found_class: type[PlinthError] | None = None,
    ) -> httpx.Response:
        """Issue a single request, with multi-region failover.

        Failover sequence:

        1. Try the primary URL.
        2. On connection error / 503 / 5xx: try each fallback in order.
        3. On 421 (Misdirected Request) or 409 + replica-redirect headers:
           extract the target URL via ``X-Plinth-Primary-URL`` (preferred)
           or look up the region named in ``X-Plinth-Primary-Region``
           against ``fallback_urls``, and retry there directly. The
           previously-primary URL is *not* re-tried — the server told us
           it's a replica.

        Each unique base URL is tried at most once (tracked via the
        ``attempted`` set) so a misconfigured pair of replicas can't
        loop forever.

        Errors from the *last* attempt are re-raised; intermediate
        failures emit a structured warning so operators see them.
        """

        candidates = self._candidates_in_order()
        if not candidates:  # pragma: no cover - constructor enforces ≥1
            raise PlinthError("no candidate URLs configured", code="INTERNAL")

        last_exc: PlinthError | None = None
        attempted: set[str] = set()
        # We may add a 409-redirect target dynamically; track the working
        # queue separately from the static candidate list.
        queue: list[tuple[str, str]] = list(candidates)
        idx = 0
        while idx < len(queue):
            region, base_url = queue[idx]
            idx += 1
            if base_url in attempted:
                continue
            attempted.add(base_url)
            client = self._client_for(base_url)
            try:
                response = self._send_one(
                    client,
                    method,
                    path,
                    json=json,
                    content=content,
                    params=params,
                    extra_headers=extra_headers,
                )
            except httpx.HTTPError as exc:
                last_exc = PlinthError(
                    f"connection error to {base_url}: {exc}",
                    code="CONNECTION_ERROR",
                )
                _logger.warning(
                    "plinth.sdk.failover: connection_error region=%s url=%s err=%s",
                    region,
                    base_url,
                    exc,
                )
                continue

            # Handle replica-redirect responses. The server emits 421
            # (Misdirected Request, RFC 7540) for read-replica writes,
            # alongside ``X-Plinth-Primary-URL`` (preferred) and
            # ``X-Plinth-Primary-Region`` headers. We also accept 409
            # for backwards compatibility with pre-spec implementations.
            if response.status_code in (421, 409) and (
                response.headers.get("X-Plinth-Primary-Region")
                or response.headers.get("X-Plinth-Primary-URL")
            ):
                target_url = self._resolve_redirect_target(response)
                target_region = (
                    response.headers.get("X-Plinth-Primary-Region", "").strip()
                    or "<primary>"
                )
                if target_url is not None and target_url not in attempted:
                    _logger.warning(
                        "plinth.sdk.failover: replica_redirect region=%s url=%s -> %s",
                        region,
                        base_url,
                        target_url,
                    )
                    # Insert the redirect target as the next item. Each
                    # URL is attempted at most once per request via the
                    # ``attempted`` set above, so loops are impossible.
                    queue.insert(idx, (target_region, target_url))
                    continue
                # No fallback for the named region: surface the error.
                self._raise_for_status(response, not_found_class=not_found_class)
                return response

            # Retry candidates on 503 / 5xx that smell like the local
            # instance being unreachable / overloaded.
            if response.status_code in (502, 503, 504) and idx < len(queue):
                last_exc = PlinthError(
                    f"upstream {response.status_code} from {base_url}",
                    code="UPSTREAM_DEGRADED",
                    response=response,
                )
                _logger.warning(
                    "plinth.sdk.failover: upstream_5xx region=%s url=%s status=%s",
                    region,
                    base_url,
                    response.status_code,
                )
                continue

            self._raise_for_status(response, not_found_class=not_found_class)
            return response

        # Every candidate failed. Re-raise the last error.
        if last_exc is not None:
            raise last_exc
        raise PlinthError("no candidate URLs succeeded", code="INTERNAL")

    def _send_one(
        self,
        client: httpx.Client,
        method: str,
        path: str,
        *,
        json: Any | None,
        content: bytes | None,
        params: dict[str, Any] | None,
        extra_headers: dict[str, str] | None,
    ) -> httpx.Response:
        """Issue the actual HTTP request through ``client``.

        Centralised so the failover loop has one entry point per attempt.
        """

        kwargs: dict[str, Any] = {
            "params": _clean_params(params),
        }
        if extra_headers is not None:
            kwargs["headers"] = extra_headers
        if method == "GET":
            return client.get(path, **kwargs)
        if method == "DELETE":
            return client.delete(path, **kwargs)
        # POST/PUT/PATCH: include json/content payloads.
        kwargs["json"] = json
        kwargs["content"] = content
        return client.request(method, path, **kwargs)

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
