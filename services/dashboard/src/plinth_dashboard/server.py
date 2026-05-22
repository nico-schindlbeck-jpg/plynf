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
from .metrics import (
    MetricsRegistry,
    metrics_middleware_factory,
    metrics_response,
)
from .overview import OverviewBuilder
from .settings import Settings, get_settings
from .timeseries import (
    InvalidMetricError,
    InvalidWindowError,
    build_timeseries,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    settings: Settings | None = None,
    *,
    embedded: bool = False,
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
    app.state.embedded = embedded
    app.state.settings = settings

    # v1.0 — Prometheus metrics. Pre-declares dashboard-specific series so
    # scrapes against a fresh deployment return the canonical schema.
    metrics = MetricsRegistry(service_name=__service__, version=__version__)
    metrics.declare_counter(
        "plinth_dashboard_polls_total",
        "Dashboard /api/* polls (per endpoint).",
    )
    metrics.declare_counter(
        "plinth_dashboard_upstream_failures_total",
        "Failed upstream calls (per upstream service).",
    )
    app.state.metrics = metrics

    app.middleware("http")(metrics_middleware_factory(metrics))
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

    @app.get("/metrics", tags=["meta"], include_in_schema=False)
    async def metrics_endpoint(request: Request):
        """Prometheus exposition endpoint.

        The dashboard service primarily serves as a proxy + UI shell, so the
        useful metrics are HTTP-level (recorded automatically by middleware)
        plus a poll counter for each ``/api/*`` route. Per-endpoint poll
        counters are bumped inline by ``_proxy``/``_proxy_mut``.
        """

        registry: MetricsRegistry = request.app.state.metrics
        return metrics_response(registry)

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

    @app.get("/api/timeseries", tags=["api"])
    async def api_timeseries(request: Request) -> JSONResponse:
        """Return a time-series payload for one of the canonical metrics.

        Query params: ``metric``, ``window`` (default ``24h``), ``buckets``
        (optional). Pulls audit events from the gateway and aggregates
        them in-process. The gateway's ``/v1/audit?limit=10000`` is the
        source of truth — anything older than what's in that buffer is
        silently dropped (operators querying multi-day windows should
        scrape Prometheus directly).
        """

        metric = (request.query_params.get("metric") or "cost").strip()
        window = (request.query_params.get("window") or "24h").strip()
        buckets_param = request.query_params.get("buckets")
        buckets_n = None
        if buckets_param:
            try:
                buckets_n = int(buckets_param)
            except (TypeError, ValueError):
                buckets_n = None

        # Reuse the overview builder's HTTP client for connection reuse.
        builder: OverviewBuilder = request.app.state.overview
        upstream = f"{gw_url}/v1/audit"
        try:
            upstream_resp = await builder.client.get(
                upstream, params={"limit": 10000}
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "code": "UPSTREAM_UNREACHABLE",
                        "message": f"Upstream {upstream} is unreachable.",
                        "details": {"reason": str(exc)},
                    }
                },
            )

        if upstream_resp.status_code >= 400:
            return JSONResponse(
                status_code=upstream_resp.status_code,
                content={
                    "error": {
                        "code": "UPSTREAM_ERROR",
                        "message": f"Upstream returned {upstream_resp.status_code}.",
                        "details": {},
                    }
                },
            )

        try:
            data = upstream_resp.json()
        except ValueError:
            data = {}
        events = (data or {}).get("events") or []

        try:
            payload = build_timeseries(
                events,
                metric=metric,
                window=window,
                buckets=buckets_n,
            )
        except (InvalidMetricError, InvalidWindowError) as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "INVALID_ARGUMENTS",
                        "message": str(exc),
                        "details": {"metric": metric, "window": window},
                    }
                },
            )
        return JSONResponse(content=payload)

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

    # v1.5 — Plinth Studio: import a workflow definition. The dashboard is
    # a thin pass-through; the workspace service does all validation. The
    # SPA POSTs to ``/api/workspaces/{ws}/workflows/import`` so the studio
    # route lives next to the read endpoints.
    @app.post(
        "/api/workspaces/{ws_id}/workflows/import",
        tags=["api"],
    )
    async def api_workspace_workflow_import(
        ws_id: str,
        request: Request,
    ) -> JSONResponse:
        return await _proxy_mut(
            request,
            method="POST",
            upstream_url=f"{ws_url}/v1/workspaces/{ws_id}/workflows/import",
            forward_body=True,
        )

    # v1.5 — Workflow replay. The replay endpoint aggregates the workflow
    # detail + audit events scoped to the workflow's workspace + the
    # workspace's snapshots so the SPA can reconstruct step state at any
    # past timestamp. This is a read-only join that lets the SPA render
    # the timeline without fanning out three separate XHRs.
    @app.get(
        "/api/workflows/{wf_id}/replay",
        tags=["api"],
    )
    async def api_workflow_replay(
        wf_id: str,
        request: Request,
    ) -> JSONResponse:
        ws_id = (request.query_params.get("ws") or "").strip()
        if not ws_id:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "INVALID_ARGUMENTS",
                        "message": "Provide ?ws=<workspace_id>",
                        "details": {},
                    }
                },
            )
        builder: OverviewBuilder = request.app.state.overview
        try:
            request.app.state.metrics.counter(
                "plinth_dashboard_polls_total",
                {"endpoint": request.url.path},
            ).inc(1)
        except Exception:  # noqa: BLE001
            pass

        wf_url = f"{ws_url}/v1/workspaces/{ws_id}/workflows/{wf_id}"
        snap_url = f"{ws_url}/v1/workspaces/{ws_id}/snapshots"
        audit_url = f"{gw_url}/v1/audit"

        async def _fetch_json(url, params=None):
            try:
                resp = await builder.client.get(url, params=params)
                if resp.status_code >= 400:
                    return None, resp.status_code
                return resp.json(), 200
            except (httpx.HTTPError, ValueError):
                return None, 502

        wf_payload, wf_status = await _fetch_json(wf_url)
        if wf_payload is None:
            return JSONResponse(
                status_code=wf_status if wf_status >= 400 else 502,
                content={
                    "error": {
                        "code": "WORKFLOW_NOT_FOUND"
                        if wf_status == 404
                        else "UPSTREAM_ERROR",
                        "message": (
                            "Workflow not found"
                            if wf_status == 404
                            else "Failed to fetch workflow."
                        ),
                        "details": {"workflow_id": wf_id, "ws_id": ws_id},
                    }
                },
            )

        # Audit + snapshots are best-effort: a failed snapshot fetch must
        # not block timeline reconstruction. The SPA degrades gracefully.
        snap_payload, _ = await _fetch_json(snap_url)
        audit_payload, _ = await _fetch_json(
            audit_url, params={"workspace_id": ws_id, "limit": 1000}
        )

        timeline = _build_workflow_timeline(wf_payload)
        return JSONResponse(
            content={
                "workflow": wf_payload,
                "snapshots": (snap_payload or {}).get("snapshots") or [],
                "audit_events": (audit_payload or {}).get("events") or [],
                "timeline": timeline,
            }
        )

    @app.get("/api/audit", tags=["api"])
    async def api_audit(request: Request) -> JSONResponse:
        return await _proxy(request, f"{gw_url}/v1/audit", forward_query=True)

    @app.get("/api/audit-stats", tags=["api"])
    async def api_audit_stats(request: Request) -> JSONResponse:
        return await _proxy(request, f"{gw_url}/v1/audit/stats", forward_query=True)

    # v1.4 — per-agent cost rollup. Forwards window/tenant_id/top params
    # straight to the gateway so the dashboard stays a thin proxy.
    @app.get("/api/cost-by-agent", tags=["api"])
    async def api_cost_by_agent(request: Request) -> JSONResponse:
        return await _proxy(
            request,
            f"{gw_url}/v1/audit/cost-by-agent",
            forward_query=True,
        )

    # v1.4 — anomaly detection results.
    @app.get("/api/anomalies", tags=["api"])
    async def api_anomalies(request: Request) -> JSONResponse:
        return await _proxy(
            request,
            f"{gw_url}/v1/audit/anomalies",
            forward_query=True,
        )

    @app.get("/api/cache-stats", tags=["api"])
    async def api_cache_stats(request: Request) -> JSONResponse:
        return await _proxy(request, f"{gw_url}/v1/cache/stats")

    @app.get("/api/tools", tags=["api"])
    async def api_tools(request: Request) -> JSONResponse:
        return await _proxy(request, f"{gw_url}/v1/tools")

    @app.get("/api/tenants", tags=["api"])
    async def api_tenants(request: Request) -> JSONResponse:
        # The identity service is the source of truth for tenant rows; we
        # fall back to the workspace if identity is unreachable so single-
        # node v0.6 deploys (no identity) still see something on /tenants.
        primary = await _proxy(request, f"{id_url}/v1/tenants")
        if primary.status_code == 200:
            return primary
        return await _proxy(request, f"{ws_url}/v1/tenants")

    @app.post("/api/tenants", tags=["api"])
    async def api_tenants_create(request: Request) -> JSONResponse:
        # Forward the body verbatim so identity does the validation.
        return await _proxy_mut(
            request,
            method="POST",
            upstream_url=f"{id_url}/v1/tenants",
            forward_body=True,
        )

    @app.get("/api/tenants/{tenant_id}", tags=["api"])
    async def api_tenant_detail(tenant_id: str, request: Request) -> JSONResponse:
        return await _proxy(request, f"{id_url}/v1/tenants/{tenant_id}")

    # v1.0 — per-tenant quotas. The dashboard proxies CRUD verbatim so the
    # admin form posts go to identity (the source of truth).
    @app.get("/api/tenants/{tenant_id}/quotas", tags=["api"])
    async def api_tenant_quotas(tenant_id: str, request: Request) -> JSONResponse:
        return await _proxy(request, f"{id_url}/v1/tenants/{tenant_id}/quotas")

    @app.post("/api/tenants/{tenant_id}/quotas", tags=["api"])
    async def api_tenant_quotas_set(
        tenant_id: str,
        request: Request,
    ) -> JSONResponse:
        return await _proxy_mut(
            request,
            method="POST",
            upstream_url=f"{id_url}/v1/tenants/{tenant_id}/quotas",
            forward_body=True,
        )

    @app.delete("/api/tenants/{tenant_id}/quotas", tags=["api"])
    async def api_tenant_quotas_reset(
        tenant_id: str,
        request: Request,
    ) -> JSONResponse:
        return await _proxy_mut(
            request,
            method="DELETE",
            upstream_url=f"{id_url}/v1/tenants/{tenant_id}/quotas",
        )

    @app.get("/api/tenants/{tenant_id}/usage", tags=["api"])
    async def api_tenant_usage(tenant_id: str, request: Request) -> JSONResponse:
        return await _proxy(request, f"{id_url}/v1/tenants/{tenant_id}/usage")

    # v1.0 — channel schema evolution wizard. The dashboard's wizard calls
    # these three endpoints; they're thin pass-throughs so the workspace
    # service stays the source of truth for validation.
    @app.post(
        "/api/workspaces/{ws_id}/channels/{name:path}/schema/check",
        tags=["api"],
    )
    async def api_channel_schema_check(
        ws_id: str,
        name: str,
        request: Request,
    ) -> JSONResponse:
        return await _proxy_mut(
            request,
            method="POST",
            upstream_url=(
                f"{ws_url}/v1/workspaces/{ws_id}/channels/{name}/schema/check"
            ),
            forward_body=True,
        )

    @app.post(
        "/api/workspaces/{ws_id}/channels/{name:path}/schema",
        tags=["api"],
    )
    async def api_channel_schema_set(
        ws_id: str,
        name: str,
        request: Request,
    ) -> JSONResponse:
        return await _proxy_mut(
            request,
            method="POST",
            upstream_url=f"{ws_url}/v1/workspaces/{ws_id}/channels/{name}/schema",
            forward_body=True,
        )

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

    # v1.5 — replay route. Same SPA shell; the JS router handles the
    # ``#/workflows/<id>/replay`` hash and renders the scrubber + per-step
    # timeline.
    @app.api_route(
        "/workflows/{wf_id}/replay",
        methods=["GET", "HEAD"],
        tags=["ui"],
    )
    async def index_workflow_replay(wf_id: str) -> FileResponse:  # noqa: ARG001
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    # v1.5 — Plinth Studio. SPA shell again; the JS router serves the
    # drag-canvas at ``#/studio``.
    @app.api_route("/studio", methods=["GET", "HEAD"], tags=["ui"])
    async def index_studio() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.api_route("/tenants", methods=["GET", "HEAD"], tags=["ui"])
    async def index_tenants() -> FileResponse:
        # v1.0 — tenants list (admin UI). SPA shell; the JS router reads
        # the URL hash and fetches /api/tenants.
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.api_route("/tenants/{tenant_id}", methods=["GET", "HEAD"], tags=["ui"])
    async def index_tenant_detail(tenant_id: str) -> FileResponse:  # noqa: ARG001
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


def _build_workflow_timeline(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive an ordered list of state-change events from a workflow row.

    The audit log doesn't carry workflow_id today, so the replay scrubber
    reconstructs step state from the workflow's own ``steps`` array. Each
    step row carries ``created_at`` / ``started_at`` / ``finished_at`` plus
    ``attempt`` and ``status``, which is enough to rebuild a per-attempt
    timeline. Events are sorted ascending by timestamp so the SPA can
    bisect by ``ts``.

    Each event has the shape::

        {
          "ts": "2026-05-10T14:00:00Z",
          "kind": "step.created" | "step.started" | "step.finished",
          "step_name": "search",
          "step_id": "step_xxx",
          "attempt": 1,
          "status": "running" | "completed" | "failed" | "cancelled",
        }

    Workflow-level events (``workflow.created`` / ``workflow.started`` /
    ``workflow.finished``) bookend the array so the scrubber knows the
    full timeline range even when no steps exist yet.
    """

    events: list[dict[str, Any]] = []
    if not isinstance(workflow, dict):
        return events

    if workflow.get("created_at"):
        events.append(
            {
                "ts": workflow["created_at"],
                "kind": "workflow.created",
                "workflow_id": workflow.get("id"),
            }
        )
    if workflow.get("started_at"):
        events.append(
            {
                "ts": workflow["started_at"],
                "kind": "workflow.started",
                "workflow_id": workflow.get("id"),
            }
        )

    for step in workflow.get("steps") or []:
        sname = step.get("name")
        sid = step.get("id")
        attempt = step.get("attempt", 1)
        if step.get("created_at"):
            events.append(
                {
                    "ts": step["created_at"],
                    "kind": "step.created",
                    "step_name": sname,
                    "step_id": sid,
                    "attempt": attempt,
                    "status": "pending",
                }
            )
        if step.get("started_at"):
            events.append(
                {
                    "ts": step["started_at"],
                    "kind": "step.started",
                    "step_name": sname,
                    "step_id": sid,
                    "attempt": attempt,
                    "status": "running",
                }
            )
        if step.get("finished_at"):
            events.append(
                {
                    "ts": step["finished_at"],
                    "kind": "step.finished",
                    "step_name": sname,
                    "step_id": sid,
                    "attempt": attempt,
                    "status": step.get("status") or "completed",
                    "error": step.get("error"),
                }
            )

    if workflow.get("finished_at"):
        events.append(
            {
                "ts": workflow["finished_at"],
                "kind": "workflow.finished",
                "workflow_id": workflow.get("id"),
                "status": workflow.get("status"),
            }
        )

    # Stable sort by ts ascending so the SPA can bisect by timestamp. Ties
    # break on event kind (created < started < finished) so a 0-duration
    # step still produces a coherent ordering.
    kind_order = {
        "workflow.created": 0,
        "step.created": 1,
        "workflow.started": 2,
        "step.started": 3,
        "step.finished": 4,
        "workflow.finished": 5,
    }
    events.sort(
        key=lambda e: (
            str(e.get("ts") or ""),
            kind_order.get(e.get("kind", ""), 99),
        )
    )
    return events


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
    # v1.0 — best-effort poll counter. The label is the dashboard endpoint
    # (request.url.path) so a Grafana panel can ``sum by (endpoint)``.
    try:
        request.app.state.metrics.counter(
            "plinth_dashboard_polls_total",
            {"endpoint": request.url.path},
        ).inc(1)
    except Exception:  # noqa: BLE001 — metrics never crash a proxy
        pass
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
        try:
            upstream_label = _extract_upstream_label(upstream_url)
            request.app.state.metrics.counter(
                "plinth_dashboard_upstream_failures_total",
                {"upstream": upstream_label},
            ).inc(1)
        except Exception:  # noqa: BLE001
            pass
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


def _extract_upstream_label(url: str) -> str:
    """Reduce an upstream URL to a service-name label.

    Maps ``http://workspace:7421/v1/...`` → ``"workspace"``. Falls back to
    the host name when the URL doesn't match a known service so the metric
    is still scrape-safe (Prometheus doesn't tolerate empty labels well).
    """

    lower = url.lower()
    if "workspace" in lower or ":7421" in lower:
        return "workspace"
    if "gateway" in lower or ":7422" in lower:
        return "gateway"
    if "identity" in lower or ":7423" in lower:
        return "identity"
    return "unknown"


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
