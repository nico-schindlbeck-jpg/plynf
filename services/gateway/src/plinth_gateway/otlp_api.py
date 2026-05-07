# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""FastAPI routes for the gateway's OTLP observability surface.

Exposes:

- ``GET  /v1/observability/status``  — emitter status snapshot (read-only).
- ``POST /v1/observability/flush``   — force-flush the buffer; admin scope.

The status endpoint requires the same inbound auth as ``/v1/cache/stats``
(bearer when configured); the flush endpoint additionally requires an admin
scope (``tenant:*:admin`` or ``*``) when JWT verification is on.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request

from .auth import check_inbound_auth
from .exceptions import Unauthorized
from .otlp_emitter import OTLPEmitter


def create_otlp_router() -> APIRouter:
    """Build the ``/v1/observability/...`` router.

    Same factory pattern as :func:`create_oauth_router`: dependencies are
    pulled off ``request.app.state`` so tests don't need a DI container.
    """
    router = APIRouter(prefix="/v1/observability", tags=["observability"])

    async def _inbound_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        if request.app.state.settings.inbound_auth_required:
            check_inbound_auth(authorization)

    def _require_admin(request: Request) -> None:
        """Require admin scope on the calling token in strict-auth modes.

        Admin = ``*`` or any ``tenant:*:admin``-style scope. In ``permissive``
        mode this is a no-op so v0.3 demos keep working.
        """
        settings = request.app.state.settings
        if settings.auth_mode == "permissive":
            return
        scopes: list[str] = list(getattr(request.state, "auth_scopes", []) or [])
        if "*" in scopes:
            return
        for scope in scopes:
            # Accept any ``tenant:<id>:admin`` (incl. wildcard ``tenant:*:admin``).
            if scope.startswith("tenant:") and scope.endswith(":admin"):
                return
        raise Unauthorized(
            "admin scope required to flush the OTLP buffer",
            details={"required": ["tenant:*:admin", "*"]},
        )

    def _emitter(request: Request) -> OTLPEmitter:
        emitter = getattr(request.app.state, "otlp", None)
        if emitter is None:
            # If lifespan didn't wire it (defensive), construct a disabled stub
            # so callers always get a coherent shape.
            emitter = OTLPEmitter(request.app.state.settings)
        return emitter

    @router.get(
        "/status",
        dependencies=[Depends(_inbound_auth)],
    )
    async def status(request: Request) -> dict:
        """Return the OTLP emitter status snapshot."""
        return _emitter(request).status

    @router.post(
        "/flush",
        dependencies=[Depends(_inbound_auth)],
    )
    async def flush(request: Request) -> dict:
        """Force-flush the OTLP buffer. Requires admin scope in strict modes."""
        _require_admin(request)
        emitter = _emitter(request)
        flushed = await emitter.flush()
        return {
            "flushed": flushed,
            "events_emitted": emitter._events_emitted,
            "flush_errors": emitter._flush_errors,
        }

    return router


__all__ = ["create_otlp_router"]
