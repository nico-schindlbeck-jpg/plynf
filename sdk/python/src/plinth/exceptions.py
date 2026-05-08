# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Typed exception hierarchy for the Plinth SDK.

Every HTTP error returned by the workspace or gateway services is mapped
to one of these classes. Each exception carries the structured ``code``,
``message``, and ``details`` from the Plinth error envelope, plus the raw
``httpx.Response`` object for callers who need the headers or raw bytes.
"""

from __future__ import annotations

from typing import Any

import httpx


class PlinthError(Exception):
    """Base class for every error raised by the Plinth SDK.

    Attributes:
        code: The machine-readable error code from the response envelope
            (e.g. ``"WORKSPACE_NOT_FOUND"``). May be ``None`` when the
            error did not originate from a Plinth service (e.g. a network
            timeout).
        message: A human-readable description of the failure.
        details: Optional structured payload returned by the service.
        response: The underlying ``httpx.Response``, when available.
    """

    code: str | None = None
    """Default error code; subclasses override this."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        response: httpx.Response | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or {}
        self.response = response

    @property
    def status_code(self) -> int | None:
        """HTTP status code from the underlying response, if any."""
        return self.response.status_code if self.response is not None else None

    def __str__(self) -> str:  # pragma: no cover - exercised via raise
        prefix = f"[{self.code}] " if self.code else ""
        return f"{prefix}{self.message}"


# ---------------------------------------------------------------------------
# 400 — validation errors
# ---------------------------------------------------------------------------


class InvalidArguments(PlinthError):
    """The request payload failed server-side validation (HTTP 400)."""

    code = "INVALID_ARGUMENTS"


# ---------------------------------------------------------------------------
# 401 — auth errors
# ---------------------------------------------------------------------------


class Unauthorized(PlinthError):
    """The API key was missing, malformed, or rejected (HTTP 401)."""

    code = "UNAUTHORIZED"


class InvalidToken(Unauthorized):
    """The bearer token's signature or structure was rejected."""

    code = "INVALID_TOKEN"


class TokenExpired(Unauthorized):
    """The bearer token has passed its ``exp`` time."""

    code = "TOKEN_EXPIRED"


class TokenRevoked(Unauthorized):
    """The bearer token's JTI is on the identity service's revocation list."""

    code = "TOKEN_REVOKED"


# ---------------------------------------------------------------------------
# 404 — not-found errors
# ---------------------------------------------------------------------------


class NotFoundError(PlinthError):
    """Base class for 404 responses, useful for ``except`` clauses."""


class ValidationError(PlinthError):
    """Base class for client-side / payload validation failures.

    A subclass of :class:`PlinthError` rather than :class:`InvalidArguments`
    so consumers can ``except ValidationError`` for "the request was
    rejected by the SDK before it ever left the box".
    """

    code = "VALIDATION_ERROR"


class WorkspaceNotFound(NotFoundError):
    """The requested workspace does not exist."""

    code = "WORKSPACE_NOT_FOUND"


class KeyNotFound(NotFoundError):
    """The requested KV key (or version) does not exist."""

    code = "KEY_NOT_FOUND"


class FileNotFound(NotFoundError):
    """The requested file path (or version) does not exist."""

    code = "FILE_NOT_FOUND"


class SnapshotNotFound(NotFoundError):
    """The requested snapshot does not exist."""

    code = "SNAPSHOT_NOT_FOUND"


class BranchNotFound(NotFoundError):
    """The requested branch does not exist."""

    code = "BRANCH_NOT_FOUND"


class ToolNotFound(NotFoundError):
    """The requested tool is not registered with the gateway."""

    code = "TOOL_NOT_FOUND"


class ChannelNotFound(NotFoundError):
    """The requested channel does not exist on the workspace."""

    code = "CHANNEL_NOT_FOUND"


class MessageNotFound(NotFoundError):
    """The requested channel message does not exist."""

    code = "MESSAGE_NOT_FOUND"


class WorkflowNotFound(NotFoundError):
    """The requested workflow does not exist."""

    code = "WORKFLOW_NOT_FOUND"


class WorkflowStepNotFound(NotFoundError):
    """The requested workflow step does not exist."""

    code = "WORKFLOW_STEP_NOT_FOUND"


class TransactionNotFound(NotFoundError):
    """The requested transaction does not exist."""

    code = "TRANSACTION_NOT_FOUND"


class TransactionInvalidStatus(InvalidArguments):
    """The transaction's lifecycle state forbids the requested operation.

    Examples: adding a call to an already-committed transaction, committing
    a non-pending one, or rolling back a fully committed transaction.

    Subclasses :class:`InvalidArguments` so existing handlers that catch
    "the request was rejected by the server" continue to work; users who
    want to disambiguate this case from generic 4xxs can ``except`` on the
    subclass.
    """

    code = "TRANSACTION_INVALID_STATUS"


class TransactionFailed(PlinthError):
    """Raised by the SDK on a catastrophic transaction commit failure.

    A *catastrophic* failure is something the engine couldn't even attempt
    to compensate (e.g. the gateway returned 5xx on the commit endpoint
    itself, the transaction record went missing mid-commit, or render-level
    errors occurred before any call ran).

    The far more common "one of the calls failed and we rolled back via
    compensation" case is NOT a :class:`TransactionFailed` — it surfaces as
    a normal :class:`~plinth.models.TransactionResult` with
    ``status='rolled_back'`` so callers can inspect the outcome.
    """

    code = "TRANSACTION_FAILED"


class InvalidWorkflowStep(InvalidArguments):
    """The workflow step is invalid (e.g. name not in the manifest).

    Subclasses :class:`InvalidArguments` so existing handlers that catch
    400-class validation errors automatically pick this up. Raised both
    client-side (when ``WorkflowHandle.start_step`` rejects an off-manifest
    name before it leaves the box) and server-side (mapped from the
    ``INVALID_WORKFLOW_STEP`` error code).
    """

    code = "INVALID_WORKFLOW_STEP"


# Backwards-compatible alias -- earlier drafts of the SDK exposed the
# client-side validation helper as :class:`InvalidStepName`. New code
# should prefer :class:`InvalidWorkflowStep` to match the server's error
# code, but anything written against the older name keeps working.
InvalidStepName = InvalidWorkflowStep


class SchemaViolation(InvalidArguments):
    """A channel send failed JSON-Schema validation against the
    channel's attached schema (v0.5).

    Subclasses :class:`InvalidArguments` so existing handlers catching
    400-class validation errors automatically pick this up. The exception
    surfaces the structured validator errors and the ID of the message the
    server filed in the dead-letter queue, so callers can immediately point
    a UI or log at the inspection endpoint.

    Attributes:
        errors: A list of ``{"message": str, "path": list, "validator": str}``
            entries from the JSON Schema validator. Always non-empty when
            raised by the SDK.
        deadletter_msg_id: The ID of the DLQ message; ``None`` if the
            server didn't surface one (rare — defensive default).
        channel: The channel the send was directed at, when known.
    """

    code = "SCHEMA_VIOLATION"

    def __init__(
        self,
        message: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        deadletter_msg_id: str | None = None,
        channel: str | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(message, **kw)
        self.errors: list[dict[str, Any]] = errors or []
        self.deadletter_msg_id = deadletter_msg_id
        self.channel = channel


# ---------------------------------------------------------------------------
# 429 — rate limiting & cost caps
# ---------------------------------------------------------------------------


class RateLimited(PlinthError):
    """The caller exceeded the service's rate limit (HTTP 429).

    Attributes:
        retry_after: Seconds the caller should wait before retrying. Parsed
            from the ``Retry-After`` HTTP header and / or the
            ``details.retry_after_seconds`` field of the error envelope.
            Defaults to ``0.0`` when neither is present.
        reason: The ``details.limit_type`` reported by the server, or the
            empty string when the gateway didn't tag it. One of
            ``"rpm"``, ``"cost_hour"``, ``"cost_day"``.
        current: Current usage at the time of rejection (when the server
            reports it).
        limit: The configured limit at the time of rejection (when the
            server reports it).
    """

    code = "RATE_LIMITED"
    http_status = 429

    def __init__(
        self,
        *args: Any,
        retry_after: float = 0.0,
        reason: str = "",
        current: Any = None,
        limit: Any = None,
        **kw: Any,
    ) -> None:
        super().__init__(*args, **kw)
        self.retry_after = retry_after
        self.reason = reason
        self.current = current
        self.limit = limit


class CostCapExceeded(RateLimited):
    """The caller blew through their hourly or daily cost cap.

    A specialised :class:`RateLimited` so existing
    ``except RateLimited`` blocks keep catching this case automatically,
    while callers who want to distinguish "I'm too fast" from "I'm too
    expensive" can branch on the subclass.
    """

    code = "COST_CAP_EXCEEDED"


# ---------------------------------------------------------------------------
# Tool-invocation failures
# ---------------------------------------------------------------------------


class ToolInvocationError(PlinthError):
    """The tool was found but its underlying invocation failed."""

    code = "TOOL_INVOCATION_FAILED"


# ---------------------------------------------------------------------------
# v0.5 — Durable workflow executor
# ---------------------------------------------------------------------------


class LeaseConflict(PlinthError):
    """A concurrent worker holds the lease, or the step isn't pending (HTTP 409)."""

    code = "LEASE_CONFLICT"


class LeaseNotHeld(PlinthError):
    """Heartbeat / release attempted on a lease this worker does not hold."""

    code = "LEASE_NOT_HELD"


class WorkerNotFound(NotFoundError):
    """The requested worker is not registered."""

    code = "WORKER_NOT_FOUND"


class NoHandlerError(PlinthError):
    """No handler is registered for the requested ``(workflow, step)`` key.

    Raised by the workflow-runtime dispatcher when a worker pulls a step
    whose name is not present in the registered handler table. Indicates a
    deployment mismatch between the worker process's handlers module and
    the workflows it's polling.
    """

    code = "NO_HANDLER"


# ---------------------------------------------------------------------------
# v0.6 — Generic resource locks
# ---------------------------------------------------------------------------


class LockConflict(PlinthError):
    """The lock is currently held by a different holder (HTTP 409).

    Raised when ``acquire`` finds the named resource already held and
    either ``wait_ms == 0`` or the wait budget elapsed before the lock
    became free.

    Attributes:
        current_holder: The holder string of whoever owns the lock right
            now (when the server reports it).
        retry_after_seconds: Hint for back-off — populated from the
            server's ``retry_after_seconds`` detail.
    """

    code = "LOCK_CONFLICT"

    def __init__(
        self,
        *args: Any,
        current_holder: str | None = None,
        retry_after_seconds: int | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(*args, **kw)
        self.current_holder = current_holder
        self.retry_after_seconds = retry_after_seconds


class LockNotHeld(PlinthError):
    """Heartbeat / release attempted on a lock the caller does not hold.

    Either the row exists but a different ``holder`` owns it (the most
    common case) or the caller's TTL elapsed and another holder stole
    the lock.
    """

    code = "LOCK_NOT_HELD"


class LockNotFound(NotFoundError):
    """The requested lock does not exist (HTTP 404)."""

    code = "LOCK_NOT_FOUND"


__all__ = [
    "BranchNotFound",
    "ChannelNotFound",
    "CostCapExceeded",
    "FileNotFound",
    "InvalidArguments",
    "InvalidStepName",
    "InvalidToken",
    "InvalidWorkflowStep",
    "KeyNotFound",
    "LeaseConflict",
    "LeaseNotHeld",
    "LockConflict",
    "LockNotFound",
    "LockNotHeld",
    "MessageNotFound",
    "NoHandlerError",
    "NotFoundError",
    "PlinthError",
    "RateLimited",
    "SchemaViolation",
    "SnapshotNotFound",
    "TokenExpired",
    "TokenRevoked",
    "ToolInvocationError",
    "ToolNotFound",
    "TransactionFailed",
    "TransactionInvalidStatus",
    "TransactionNotFound",
    "Unauthorized",
    "ValidationError",
    "WorkerNotFound",
    "WorkflowNotFound",
    "WorkflowStepNotFound",
    "WorkspaceNotFound",
]
