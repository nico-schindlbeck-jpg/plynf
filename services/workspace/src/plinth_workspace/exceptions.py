# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Typed domain exceptions and FastAPI handlers for the workspace service.

All exceptions map to the standard error envelope from ``CONTRACTS.md``::

    {"error": {"code": "...", "message": "...", "details": {...}}}
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class PlinthError(Exception):
    """Base exception for all workspace-service domain errors."""

    code: str = "INTERNAL_ERROR"
    status_code: int = 500
    message: str = "internal error"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details: dict[str, Any] = dict(details or {})


# ---------------------------------------------------------------------------
# 404 family


class WorkspaceNotFound(PlinthError):
    code = "WORKSPACE_NOT_FOUND"
    status_code = 404
    message = "workspace not found"

    def __init__(self, workspace_id: str) -> None:
        super().__init__(
            f"Workspace {workspace_id} does not exist",
            details={"workspace_id": workspace_id},
        )


class KeyNotFound(PlinthError):
    code = "KEY_NOT_FOUND"
    status_code = 404
    message = "key not found"

    def __init__(self, workspace_id: str, key: str, version: int | None = None) -> None:
        details: dict[str, Any] = {"workspace_id": workspace_id, "key": key}
        if version is not None:
            details["version"] = version
        super().__init__(
            f"Key {key!r} not found in workspace {workspace_id}",
            details=details,
        )


class FileNotFound(PlinthError):
    code = "FILE_NOT_FOUND"
    status_code = 404
    message = "file not found"

    def __init__(self, workspace_id: str, path: str, version: int | None = None) -> None:
        details: dict[str, Any] = {"workspace_id": workspace_id, "path": path}
        if version is not None:
            details["version"] = version
        super().__init__(
            f"File {path!r} not found in workspace {workspace_id}",
            details=details,
        )


class SnapshotNotFound(PlinthError):
    code = "SNAPSHOT_NOT_FOUND"
    status_code = 404
    message = "snapshot not found"

    def __init__(self, snapshot_id: str) -> None:
        super().__init__(
            f"Snapshot {snapshot_id} does not exist",
            details={"snapshot_id": snapshot_id},
        )


class BranchNotFound(PlinthError):
    code = "BRANCH_NOT_FOUND"
    status_code = 404
    message = "branch not found"

    def __init__(self, branch_id: str) -> None:
        super().__init__(
            f"Branch {branch_id} does not exist",
            details={"branch_id": branch_id},
        )


class ChannelNotFound(PlinthError):
    code = "CHANNEL_NOT_FOUND"
    status_code = 404
    message = "channel not found"

    def __init__(self, workspace_id: str, name: str) -> None:
        super().__init__(
            f"Channel {name!r} not found in workspace {workspace_id}",
            details={"workspace_id": workspace_id, "channel": name},
        )


class MessageNotFound(PlinthError):
    code = "MESSAGE_NOT_FOUND"
    status_code = 404
    message = "channel message not found"

    def __init__(
        self,
        workspace_id: str,
        channel_name: str,
        message_id: str,
    ) -> None:
        super().__init__(
            f"Message {message_id} not found in channel {channel_name!r}",
            details={
                "workspace_id": workspace_id,
                "channel": channel_name,
                "message_id": message_id,
            },
        )


class WorkflowNotFound(PlinthError):
    code = "WORKFLOW_NOT_FOUND"
    status_code = 404
    message = "workflow not found"

    def __init__(self, workflow_id: str) -> None:
        super().__init__(
            f"Workflow {workflow_id} does not exist",
            details={"workflow_id": workflow_id},
        )


class WorkflowStepNotFound(PlinthError):
    code = "WORKFLOW_STEP_NOT_FOUND"
    status_code = 404
    message = "workflow step not found"

    def __init__(self, workflow_id: str, step_id: str) -> None:
        super().__init__(
            f"Step {step_id} not found in workflow {workflow_id}",
            details={"workflow_id": workflow_id, "step_id": step_id},
        )


# ---------------------------------------------------------------------------
# 4xx other


class InvalidArguments(PlinthError):
    code = "INVALID_ARGUMENTS"
    status_code = 400
    message = "invalid arguments"


class InvalidWorkflowStep(InvalidArguments):
    """Raised when a step name is not in the manifest, or a status
    transition is invalid (e.g. completing an already-cancelled step)."""

    code = "INVALID_WORKFLOW_STEP"
    status_code = 400
    message = "invalid workflow step"

    def __init__(
        self,
        workflow_id: str,
        step_name: str | None = None,
        manifest: list[str] | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        details: dict[str, Any] = {"workflow_id": workflow_id}
        if step_name is not None:
            details["step_name"] = step_name
        if manifest is not None:
            details["manifest"] = manifest
        if reason is not None:
            details["reason"] = reason

        if reason is not None:
            msg = f"Invalid workflow step for {workflow_id}: {reason}"
        elif step_name is not None:
            msg = (
                f"Step {step_name!r} is not in the manifest of "
                f"workflow {workflow_id}"
            )
        else:
            msg = f"Invalid workflow step for {workflow_id}"

        super().__init__(msg, details=details)




class Unauthorized(PlinthError):
    code = "UNAUTHORIZED"
    status_code = 401
    message = "missing or invalid bearer token"


class SchemaViolation(InvalidArguments):
    """Raised when a channel send (or a DLQ replay) fails JSON Schema
    validation against the channel's attached schema.

    Maps to HTTP 422 with the standard envelope::

        {
          "error": {
            "code": "SCHEMA_VIOLATION",
            "message": "...",
            "details": {
              "errors": [{"message": "...", "path": [...]}],
              "deadletter_msg_id": "msg_...",
              "channel": "..."
            }
          }
        }
    """

    code = "SCHEMA_VIOLATION"
    status_code = 422
    message = "payload does not match channel schema"

    def __init__(
        self,
        channel: str,
        errors: list[dict[str, Any]],
        *,
        deadletter_msg_id: str | None = None,
        workspace_id: str | None = None,
    ) -> None:
        details: dict[str, Any] = {"channel": channel, "errors": errors}
        if deadletter_msg_id is not None:
            details["deadletter_msg_id"] = deadletter_msg_id
        if workspace_id is not None:
            details["workspace_id"] = workspace_id
        super().__init__(
            f"Payload does not match schema for channel {channel!r}",
            details=details,
        )


# ---------------------------------------------------------------------------
# Branch-state errors (operational, not 4xx-but-not-found)


class BranchAlreadyMerged(PlinthError):
    code = "BRANCH_ALREADY_MERGED"
    status_code = 400
    message = "branch already merged"

    def __init__(self, branch_id: str) -> None:
        super().__init__(
            f"Branch {branch_id} has already been merged",
            details={"branch_id": branch_id},
        )


# ---------------------------------------------------------------------------
# v0.6 — Generic resource locks


class LockHeld(PlinthError):
    """Raised when a lock is currently held by a different holder."""

    code = "LOCK_HELD"
    status_code = 409
    message = "lock is currently held"

    def __init__(
        self,
        workspace_id: str,
        name: str,
        *,
        current_holder: str | None = None,
        retry_after_seconds: int | None = None,
        expires_at: str | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "workspace_id": workspace_id,
            "name": name,
        }
        if current_holder is not None:
            details["current_holder"] = current_holder
        if retry_after_seconds is not None:
            details["retry_after_seconds"] = retry_after_seconds
        if expires_at is not None:
            details["expires_at"] = expires_at
        super().__init__(
            f"Lock {name!r} on workspace {workspace_id} is currently held",
            details=details,
        )


class LockNotHeld(PlinthError):
    """Raised when a heartbeat / release is attempted on a lock the
    caller does not currently hold.

    This may happen because:
    - the lock exists but is held by a different ``holder``, or
    - the caller's TTL elapsed and the reaper expired the row before
      the heartbeat arrived (so the next acquire winner is now the
      holder).
    """

    code = "LOCK_NOT_HELD"
    status_code = 409
    message = "lock is not held by this caller"

    def __init__(
        self,
        workspace_id: str,
        name: str,
        *,
        holder: str | None = None,
        actual_holder: str | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "workspace_id": workspace_id,
            "name": name,
        }
        if holder is not None:
            details["holder"] = holder
        if actual_holder is not None:
            details["actual_holder"] = actual_holder
        super().__init__(
            f"Lock {name!r} on workspace {workspace_id} not held by caller",
            details=details,
        )


class LockNotFound(PlinthError):
    """Raised on a GET / heartbeat against a lock that doesn't exist."""

    code = "LOCK_NOT_FOUND"
    status_code = 404
    message = "lock not found"

    def __init__(self, workspace_id: str, name: str) -> None:
        super().__init__(
            f"Lock {name!r} not found in workspace {workspace_id}",
            details={"workspace_id": workspace_id, "name": name},
        )


# ---------------------------------------------------------------------------
# v0.6 — Migration rollback errors


class MigrationRollbackMissing(PlinthError):
    """Raised when a rollback file is required but absent.

    Carries the list of migration IDs that lack rollback files so the
    HTTP envelope's ``details.missing`` is structured.
    """

    code = "MIGRATION_ROLLBACK_MISSING"
    status_code = 400
    message = "rollback file is missing for one or more migrations"

    def __init__(self, missing_ids: list[str]) -> None:
        super().__init__(
            f"Rollback files missing for: {', '.join(missing_ids)}",
            details={"missing": list(missing_ids)},
        )


class MigrationRollbackFailed(PlinthError):
    """Raised when executing a rollback file errors.

    ``details.migration_id`` identifies which migration's rollback failed
    and ``details.error`` carries the underlying error message.
    """

    code = "MIGRATION_ROLLBACK_FAILED"
    status_code = 500
    message = "rollback execution failed"

    def __init__(self, migration_id: str, error: str) -> None:
        super().__init__(
            f"Rollback for {migration_id!r} failed: {error}",
            details={"migration_id": migration_id, "error": error},
        )


# ---------------------------------------------------------------------------
# Response helpers


def _envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


async def plinth_exception_handler(_request: Request, exc: PlinthError) -> JSONResponse:
    """Map :class:`PlinthError` to the contract error envelope."""

    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(exc.code, exc.message, exc.details),
    )


async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Wrap built-in HTTPException raises in the same envelope."""

    code = "HTTP_ERROR"
    if exc.status_code == 404:
        code = "NOT_FOUND"
    elif exc.status_code == 401:
        code = "UNAUTHORIZED"
    elif exc.status_code == 400:
        code = "INVALID_ARGUMENTS"
    elif exc.status_code == 405:
        code = "METHOD_NOT_ALLOWED"
    elif exc.status_code == 429:
        code = "RATE_LIMITED"
    elif exc.status_code >= 500:
        code = "INTERNAL_ERROR"

    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code, detail or exc.__class__.__name__),
    )


async def validation_exception_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Map FastAPI/Pydantic validation errors to the contract envelope."""

    return JSONResponse(
        status_code=400,
        content=_envelope(
            "INVALID_ARGUMENTS",
            "request validation failed",
            {"errors": exc.errors()},
        ),
    )


async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Last-chance catch-all that still respects the error envelope."""

    return JSONResponse(
        status_code=500,
        content=_envelope("INTERNAL_ERROR", str(exc) or "unhandled error"),
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Register all error handlers on the FastAPI app."""

    app.add_exception_handler(PlinthError, plinth_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
