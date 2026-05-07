# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Typed domain exceptions and FastAPI handlers for the identity service.

Mirrors the standard error envelope from ``CONTRACTS.md``::

    {"error": {"code": "...", "message": "...", "details": {...}}}
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class IdentityError(Exception):
    """Base exception for all identity-service domain errors."""

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


class TokenNotFound(IdentityError):
    code = "TOKEN_NOT_FOUND"
    status_code = 404
    message = "token not found"

    def __init__(self, jti: str) -> None:
        super().__init__(
            f"Token {jti} does not exist",
            details={"jti": jti},
        )


class InvalidToken(IdentityError):
    code = "INVALID_TOKEN"
    status_code = 401
    message = "token signature or structure is invalid"


class TokenExpired(IdentityError):
    code = "TOKEN_EXPIRED"
    status_code = 401
    message = "token has expired"


class TokenRevoked(IdentityError):
    code = "TOKEN_REVOKED"
    status_code = 401
    message = "token has been revoked"


class InvalidArguments(IdentityError):
    code = "INVALID_ARGUMENTS"
    status_code = 400
    message = "invalid arguments"


class TenantNotFound(IdentityError):
    code = "TENANT_NOT_FOUND"
    status_code = 404
    message = "tenant not found"

    def __init__(self, tenant_id: str) -> None:
        super().__init__(
            f"Tenant {tenant_id!r} does not exist",
            details={"tenant_id": tenant_id},
        )


class SigningKeyNotFound(IdentityError):
    code = "SIGNING_KEY_NOT_FOUND"
    status_code = 404
    message = "signing key not found"

    def __init__(self, kid: str) -> None:
        super().__init__(
            f"Signing key {kid!r} does not exist",
            details={"kid": kid},
        )


class TenantAlreadyExists(IdentityError):
    code = "TENANT_ALREADY_EXISTS"
    status_code = 409
    message = "tenant already exists"

    def __init__(self, tenant_id: str) -> None:
        super().__init__(
            f"Tenant {tenant_id!r} already exists",
            details={"tenant_id": tenant_id},
        )


def _envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


async def identity_exception_handler(_request: Request, exc: IdentityError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(exc.code, exc.message, exc.details),
    )


async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code_map = {
        400: "INVALID_ARGUMENTS",
        401: "UNAUTHORIZED",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        429: "RATE_LIMITED",
    }
    code = code_map.get(exc.status_code, "HTTP_ERROR")
    if exc.status_code >= 500:
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
    return JSONResponse(
        status_code=400,
        content=_envelope(
            "INVALID_ARGUMENTS",
            "request validation failed",
            {"errors": exc.errors()},
        ),
    )


async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=_envelope("INTERNAL_ERROR", str(exc) or "unhandled error"),
    )


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(IdentityError, identity_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
