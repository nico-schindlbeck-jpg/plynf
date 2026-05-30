# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Dialect-aware HTTP error envelopes.

Plynf fronts many vendor APIs, and each SDK parses *errors* in its own shape.
A client that points its base URL at Plynf must therefore see *its own* error
shape on a 401/402/404 — otherwise its error handling breaks, defeating the
same "no code change" promise the success-path translators keep. FastAPI's
default ``{"detail": ...}`` matches no vendor, so :func:`error_body` reshapes a
status code + detail into the envelope that matches the request's front door:

* **OpenAI / Azure / Responses** — ``{"error": {message, type, code, param}}``
* **Anthropic** — ``{"type": "error", "error": {type, message}}``
* **Gemini / Vertex (Google)** — ``{"error": {code, message, status}}``
* **Cohere / Bedrock** — ``{"message": ...}``

Structured detail (e.g. the tier-limit ``{error, reason, tier, upgrade_hint}``)
is preserved inside the envelope so callers keep the upgrade hint.
"""

from __future__ import annotations

from typing import Any

# Status code → vendor error "type"/"status" string.
_OPENAI_TYPE = {
    400: "invalid_request_error",
    401: "invalid_request_error",
    403: "invalid_request_error",
    404: "invalid_request_error",
    402: "insufficient_quota",
    429: "rate_limit_error",
}
_ANTHROPIC_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    402: "permission_error",
    429: "rate_limit_error",
}
_GOOGLE_STATUS = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    402: "FAILED_PRECONDITION",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    502: "UNAVAILABLE",
    503: "UNAVAILABLE",
}


def _split_detail(detail: Any) -> tuple[str, dict[str, Any]]:
    """Return ``(message, extra)`` from an ``HTTPException.detail``.

    A string detail is the message with no extras. A dict detail (e.g. the
    tier-limit payload) yields a human ``message`` plus the remaining fields
    so no structured context is lost.
    """
    if isinstance(detail, dict):
        message = (
            detail.get("message")
            or detail.get("reason")
            or detail.get("error")
            or "error"
        )
        extra = {k: v for k, v in detail.items() if k != "message"}
        return str(message), extra
    return str(detail), {}


def _dialect_for_path(path: str) -> str:
    """Classify a request path into a vendor dialect for error shaping."""
    if path.startswith("/v1/messages") or "/publishers/anthropic/" in path:
        return "anthropic"
    if (
        path.startswith("/v1beta/")
        or "/publishers/google/" in path
        or path.endswith(":generateContent")
    ):
        return "gemini"
    if path.startswith("/v2/chat"):
        return "cohere"
    if path.startswith("/model/") or path.endswith("/converse"):
        return "bedrock"
    return "openai"


def error_body(path: str, status_code: int, detail: Any) -> dict[str, Any]:
    """Build the dialect-appropriate error envelope for ``path``."""
    message, extra = _split_detail(detail)
    dialect = _dialect_for_path(path)

    if dialect == "anthropic":
        etype = _ANTHROPIC_TYPE.get(
            status_code, "api_error" if status_code >= 500 else "invalid_request_error"
        )
        return {"type": "error", "error": {"type": etype, "message": message, **extra}}

    if dialect == "gemini":
        return {
            "error": {
                "code": status_code,
                "message": message,
                "status": _GOOGLE_STATUS.get(status_code, "UNKNOWN"),
                **extra,
            }
        }

    if dialect in ("cohere", "bedrock"):
        return {"message": message, **extra}

    # OpenAI (default). Promote a structured ``error`` field to ``code``.
    extra = dict(extra)
    code = extra.pop("error", None)
    etype = _OPENAI_TYPE.get(
        status_code, "api_error" if status_code >= 500 else "invalid_request_error"
    )
    err: dict[str, Any] = {"message": message, "type": etype, "param": None, "code": code}
    err.update(extra)
    return {"error": err}


__all__ = ["error_body"]
