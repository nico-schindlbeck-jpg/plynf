# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""FastAPI app for the Plinth dashboard service.

The dashboard is read-only:

- ``GET /``                 → serves ``static/index.html``
- ``GET /static/{path}``    → static SPA assets (CSS / JS / favicon)
- ``GET /healthz``          → liveness probe
- ``GET /api/overview``     → JSON aggregation across workspace + gateway
- ``GET /api/workflows/overview`` → cross-workspace workflow aggregation
- ``GET /api/workspaces``   → proxy to workspace service
- ``GET /api/workspaces/{ws_id}`` → proxy
- ``GET /api/workspaces/{ws_id}/kv`` → proxy
- ``GET /api/workspaces/{ws_id}/snapshots`` → proxy
- ``GET /api/workspaces/{ws_id}/channels`` → proxy
- ``GET /api/workspaces/{ws_id}/workflows`` → proxy
- ``GET /api/workspaces/{ws_id}/workflows/{wf_id}`` → proxy (single workflow)
- ``GET /api/audit``        → proxy to gateway service
- ``GET /api/cache-stats``  → proxy
- ``GET /api/audit-stats``  → proxy
- ``GET /api/tools``        → proxy
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __service__, __version__
from .logging_config import configure_logging, get_logger
from .overview import OverviewBuilder
from .settings import Settings, get_settings

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    settings: Settings | None = None,
    *,
    overview: OverviewBuilder | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    Args:
        settings: Optional settings override (tests pass a custom one).
        overview: Optional pre-built :class:`OverviewBuilder`. Tests inject
            an ``httpx.AsyncClient`` mocked with ``respx``.
    """
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log = get_logger()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if overview is None:
            app.state.overview = OverviewBuilder(settings)
            app.state._owns_overview = True
        else:
            app.state.overview = overview
            app.state._owns_overview = False
        log.info(
            "dashboard.startup",
            port=settings.port,
            workspace_url=settings.workspace_url,
            gateway_url=settings.gateway_url,
        )
        try:
            yield
        finally:
            if app.state._owns_overview:
                await app.state.overview.aclose()
            log.info("dashboard.shutdown")

    app = FastAPI(
        title="plinth-dashboard",
        version=__version__,
        description="Plinth dashboard — read-only observability UI.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.middleware("http")(_request_context_middleware)

    # Static SPA: vanilla HTML/CSS/JS, no build step.
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    _register_routes(app, settings)
    return app


# ---------------------------------------------------------------------------
# Routes


def _register_routes(app: FastAPI, settings: Settings) -> None:
    ws_url = settings.workspace_url.rstrip("/")
    gw_url = settings.gateway_url.rstrip("/")
    id_url = settings.identity_url.rstrip("/")

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict:
        return {"status": "ok", "version": __version__, "service": __service__}

    @app.get("/api/overview", tags=["api"])
    async def api_overview(request: Request) -> JSONResponse:
        builder: OverviewBuilder = request.app.state.overview
        try:
            data = await builder.build()
        except Exception as exc:  # noqa: BLE001 — last-resort guard
            get_logger().error("dashboard.overview.failed", error=str(exc))
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "Failed to build dashboard overview.",
                        "details": {"reason": str(exc)},
                    }
                },
            )
        return JSONResponse(content=data)

    @app.get("/api/workflows/overview", tags=["api"])
    async def api_workflows_overview(request: Request) -> JSONResponse:
        """Aggregate workflows across all visible workspaces.

        For each workspace listed by the workspace service, fetch its
        ``/workflows`` and roll the result up into a single payload the SPA
        renders into the cross-workspace workflow list. Failures degrade
        gracefully (``partial: true``) so a single slow workspace never
        breaks the page.
        """

        builder: OverviewBuilder = request.app.state.overview
        try:
            data = await builder.build_workflows_overview()
        except Exception as exc:  # noqa: BLE001 — last-resort guard
            get_logger().error(
                "dashboard.workflows_overview.failed", error=str(exc)
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "Failed to build workflows overview.",
                        "details": {"reason": str(exc)},
                    }
                },
            )
        return JSONResponse(content=data)

    # --------------------------------------------------------------- proxies

    @app.get("/api/workspaces", tags=["api"])
    async def api_workspaces(request: Request) -> JSONResponse:
        return await _proxy(request, f"{ws_url}/v1/workspaces")

    @app.get("/api/workspaces/{ws_id}", tags=["api"])
    async def api_workspace(ws_id: str, request: Request) -> JSONResponse:
        return await _proxy(request, f"{ws_url}/v1/workspaces/{ws_id}")

    @app.get("/api/workspaces/{ws_id}/kv", tags=["api"])
    async def api_workspace_kv(ws_id: str, request: Request) -> JSONResponse:
        return await _proxy(request, f"{ws_url}/v1/workspaces/{ws_id}/kv")

    @app.get("/api/workspaces/{ws_id}/snapshots", tags=["api"])
    async def api_workspace_snapshots(ws_id: str, request: Request) -> JSONResponse:
        return await _proxy(request, f"{ws_url}/v1/workspaces/{ws_id}/snapshots")

    @app.get("/api/workspaces/{ws_id}/channels", tags=["api"])
    async def api_workspace_channels(ws_id: str, request: Request) -> JSONResponse:
        return await _proxy(request, f"{ws_url}/v1/workspaces/{ws_id}/channels")

    @app.get("/api/workspaces/{ws_id}/channels/{name:path}/deadletter", tags=["api"])
    async def api_workspace_channel_deadletter(
        ws_id: str,
        name: str,
        request: Request,
    ) -> JSONResponse:
        return await _proxy(
            request,
            f"{ws_url}/v1/workspaces/{ws_id}/channels/{name}/deadletter",
            forward_query=True,
        )

    # v0.6 — DLQ batch ops. Both routes are explicitly POST/DELETE so the
    # SPA can't accidentally trigger them via a stray ``<a href>``. We
    # forward the body (replay-all) and query (purge) verbatim — the
    # workspace service is the source of truth for argument validation.
    @app.post(
        "/api/workspaces/{ws_id}/channels/{name:path}/deadletter/replay-all",
        tags=["api"],
    )
    async def api_workspace_channel_replay_all(
        ws_id: str,
        name: str,
        request: Request,
    ) -> JSONResponse:
        return await _proxy_mut(
            request,
            method="POST",
            upstream_url=f"{ws_url}/v1/workspaces/{ws_id}/channels/{name}/deadletter/replay-all",
            forward_query=True,
            forward_body=True,
        )

    @app.delete(
        "/api/workspaces/{ws_id}/channels/{name:path}/deadletter",
        tags=["api"],
    )
    async def api_workspace_channel_purge_dlq(
        ws_id: str,
        name: str,
        request: Request,
    ) -> JSONResponse:
        return await _proxy_mut(
            request,
            method="DELETE",
            upstream_url=f"{ws_url}/v1/workspaces/{ws_id}/channels/{name}/deadletter",
            forward_query=True,
            forward_body=False,
        )

    @app.get("/api/workspaces/{ws_id}/workflows", tags=["api"])
    async def api_workspace_workflows(ws_id: str, request: Request) -> JSONResponse:
        return await _proxy(request, f"{ws_url}/v1/workspaces/{ws_id}/workflows")

    @app.get("/api/workspaces/{ws_id}/workflows/{wf_id}", tags=["api"])
    async def api_workspace_workflow_detail(
        ws_id: str,
        wf_id: str,
        request: Request,
    ) -> JSONResponse:
        return await _proxy(
            request,
            f"{ws_url}/v1/workspaces/{ws_id}/workflows/{wf_id}",
        )

    @app.get("/api/audit", tags=["api"])
    async def api_audit(request: Request) -> JSONResponse:
        return await _proxy(request, f"{gw_url}/v1/audit", forward_query=True)

    @app.get("/api/audit-stats", tags=["api"])
    async def api_audit_stats(request: Request) -> JSONResponse:
        return await _proxy(request, f"{gw_url}/v1/audit/stats", forward_query=True)

    @app.get("/api/cache-stats", tags=["api"])
    async def api_cache_stats(request: Request) -> JSONResponse:
        return await _proxy(request, f"{gw_url}/v1/cache/stats")

    @app.get("/api/tools", tags=["api"])
    async def api_tools(request: Request) -> JSONResponse:
        return await _proxy(request, f"{gw_url}/v1/tools")

    @app.get("/api/tenants", tags=["api"])
    async def api_tenants(request: Request) -> JSONResponse:
        # Defer to the workspace's tenants list — the dashboard merges with
        # the gateway view inside ``/api/overview``. Keeping the dedicated
        # endpoint pointed at the workspace mirrors the contract: tenant IDs
        # are owned by the workspace service first.
        return await _proxy(request, f"{ws_url}/v1/tenants")

    @app.get("/api/identity/tokens", tags=["api"])
    async def api_identity_tokens(request: Request) -> JSONResponse:
        # The identity service returns metadata only (``GET /v1/tokens/{jti}``).
        # We deliberately do NOT expose any endpoint that returns the JWT body.
        # ``agent_id`` is a query param used to scope the listing once the
        # identity service supports it; for v0.3 the dashboard simply forwards
        # the request and the response is whatever the upstream returns.
        jti = request.query_params.get("jti")
        if jti:
            return await _proxy(request, f"{id_url}/v1/tokens/{jti}")
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "INVALID_ARGUMENTS",
                    "message": "Provide ?jti=jti_xxx to look up a token.",
                    "details": {},
                }
            },
        )

    # --------------------------------------------------------------- UI

    @app.api_route("/", methods=["GET", "HEAD"], tags=["ui"])
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.api_route("/workspaces/{ws_id}", methods=["GET", "HEAD"], tags=["ui"])
    async def index_workspace(ws_id: str) -> FileResponse:  # noqa: ARG001
        # Sub-routes serve the same SPA shell; the JS router reads the URL.
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.api_route("/workflows", methods=["GET", "HEAD"], tags=["ui"])
    async def index_workflows() -> FileResponse:
        # The cross-workspace workflow list is rendered by the SPA based on
        # the hash route; serving the shell from this path means a hard
        # refresh on /workflows still loads the dashboard.
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.api_route("/workflows/{wf_id}", methods=["GET", "HEAD"], tags=["ui"])
    async def index_workflow_detail(wf_id: str) -> FileResponse:  # noqa: ARG001
        # Same shell, different hash. The SPA reads ?ws=<ws_id> from the URL
        # to know which workspace to query.
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


async def _proxy_mut(
    request: Request,
    *,
    method: str,
    upstream_url: str,
    forward_query: bool = False,
    forward_body: bool = False,
) -> JSONResponse:
    """Forward a mutating (POST/DELETE) call upstream.

    Mirrors :func:`_proxy` but for verbs other than GET. The body is
    forwarded verbatim when ``forward_body=True`` so the workspace
    service (the source of truth) gets to validate the payload — the
    dashboard is a thin transport layer here, not a policy point.
    """

    builder: OverviewBuilder = request.app.state.overview
    params: dict[str, Any] | None = None
    if forward_query and request.query_params:
        params = dict(request.query_params)

    body: bytes | None = None
    if forward_body:
        # Reading the body once consumes the stream; FastAPI does not
        # need to re-read it because we already routed through this
        # handler. A genuinely empty body becomes ``None`` so httpx
        # sends a content-length:0 with no JSON header.
        raw = await request.body()
        body = raw or None

    headers: dict[str, str] = {}
    # Pass through the JSON content-type for body-bearing calls so the
    # upstream pydantic body parser engages. We *don't* forward auth
    # headers — the dashboard runs unauthenticated against an
    # unauthenticated workspace deployment.
    if body is not None and request.headers.get("content-type"):
        headers["content-type"] = request.headers["content-type"]

    try:
        upstream = await builder.client.request(
            method,
            upstream_url,
            params=params,
            content=body,
            headers=headers or None,
        )
    except httpx.HTTPError as exc:
        get_logger().warning(
            "dashboard.proxy.unreachable",
            upstream=upstream_url,
            method=method,
            error=str(exc),
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "UPSTREAM_UNREACHABLE",
                    "message": f"Upstream {upstream_url} is unreachable.",
                    "details": {"reason": str(exc)},
                }
            },
        )

    try:
        body_json = upstream.json()
    except ValueError:
        return JSONResponse(
            status_code=upstream.status_code,
            content={
                "error": {
                    "code": "UPSTREAM_NON_JSON",
                    "message": "Upstream returned a non-JSON response.",
                    "details": {"status": upstream.status_code},
                }
            },
        )
    return JSONResponse(status_code=upstream.status_code, content=body_json)


async def _proxy(
    request: Request,
    upstream_url: str,
    *,
    forward_query: bool = False,
) -> JSONResponse:
    """Forward a GET to an upstream service using the dashboard's client.

    Returns the upstream JSON body, or a 502 if the upstream is unreachable.
    """
    builder: OverviewBuilder = request.app.state.overview
    params: dict[str, Any] | None = None
    if forward_query and request.query_params:
        params = dict(request.query_params)
    try:
        upstream = await builder.client.get(upstream_url, params=params)
    except httpx.HTTPError as exc:
        get_logger().warning(
            "dashboard.proxy.unreachable",
            upstream=upstream_url,
            error=str(exc),
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "UPSTREAM_UNREACHABLE",
                    "message": f"Upstream {upstream_url} is unreachable.",
                    "details": {"reason": str(exc)},
                }
            },
        )

    # Best-effort JSON pass-through; non-JSON bodies become an error envelope
    # so the SPA can still render something coherent.
    try:
        body = upstream.json()
    except ValueError:
        return JSONResponse(
            status_code=upstream.status_code,
            content={
                "error": {
                    "code": "UPSTREAM_NON_JSON",
                    "message": "Upstream returned a non-JSON response.",
                    "details": {"status": upstream.status_code},
                }
            },
        )
    return JSONResponse(status_code=upstream.status_code, content=body)


# ---------------------------------------------------------------------------
# Middleware


async def _request_context_middleware(request: Request, call_next):
    """Attach a request_id to log context for every request."""
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        service=__service__,
        request_id=request_id,
        path=request.url.path,
        method=request.method,
    )
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


# ---------------------------------------------------------------------------
# Module-level default app for ``python -m plinth_dashboard`` and uvicorn.

_app: FastAPI | None = None


def __getattr__(name: str):
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)


__all__ = ["create_app", "STATIC_DIR"]
