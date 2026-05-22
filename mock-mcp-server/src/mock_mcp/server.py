# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""FastAPI app for the Mock MCP Server.

Exposes:

* ``GET /healthz`` — liveness/version probe.
* ``GET /tools`` — full tool registry metadata.
* ``POST /invoke/{tool_name}`` — dispatch a tool call.

The app keeps two pieces of process-local state:

* A shared :class:`httpx.AsyncClient` (lifespan-managed).
* The in-memory list of notes for ``notes.add`` / ``notes.list``.

Path traversal protection, scheme validation and error mapping live in
:mod:`mock_mcp.tools`; the route layer is intentionally thin.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .logging_config import configure_logging, get_logger
from .metrics import (
    MetricsRegistry,
    metrics_middleware_factory,
    metrics_response,
)
from .settings import Settings, get_settings
from .tools import TOOL_LIST, TOOL_REGISTRY, ToolContext, ToolError


def _build_error(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the canonical Plinth error envelope."""
    return {"error": {"code": code, "message": message, "details": details or {}}}


def _record_mcp_invocation(
    registry: MetricsRegistry | None,
    tool: str,
    start: float,
    *,
    ok: bool,
) -> None:
    """Record an MCP invocation outcome on the registry.

    Best-effort: any failure here is swallowed so a metrics bug never
    breaks an invocation. Increments the canonical
    ``plinth_mcp_invocations_total`` and (on errors) the matching
    ``_errors_total``, plus observes the duration histogram.
    """
    if registry is None:
        return
    try:
        elapsed = max(0.0, time.perf_counter() - start)
        registry.counter(
            "plinth_mcp_invocations_total",
            {"tool": tool, "result": "ok" if ok else "error"},
        ).inc(1)
        if not ok:
            registry.counter(
                "plinth_mcp_invocation_errors_total",
                {"tool": tool},
            ).inc(1)
        registry.histogram(
            "plinth_mcp_invocation_duration_seconds",
            {"tool": tool},
        ).observe(elapsed)
    except Exception:  # noqa: BLE001 — metrics must never crash invoke
        pass


def create_app(
    settings: Settings | None = None,
    *,
    embedded: bool = False,
) -> FastAPI:
    """Build a FastAPI app instance.

    Args:
        settings: Optional explicit settings override (used by tests).
            Falls back to ``Settings()`` from the environment.
    """
    if settings is None:
        settings = get_settings()

    configure_logging(settings.log_level, settings.log_format)
    log = get_logger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Set up shared resources for the duration of the app."""
        settings.fixtures_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient() as client:
            app.state.http_client = client
            app.state.notes = []
            app.state.settings = settings
            log.info(
                "mock-mcp.startup",
                port=settings.port,
                fixtures_dir=str(settings.fixtures_dir),
                tools=len(TOOL_LIST),
            )
            yield
        log.info("mock-mcp.shutdown")

    app = FastAPI(
        title="Plinth Mock MCP Server",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.embedded = embedded

    # v1.0 — Prometheus metrics. Pre-declared so a fresh server returns the
    # canonical schema even before any tool has been invoked.
    metrics = MetricsRegistry(service_name="mock-mcp", version=__version__)
    metrics.declare_counter(
        "plinth_mcp_invocations_total",
        "Total MCP tool invocations.",
    )
    metrics.declare_counter(
        "plinth_mcp_invocation_errors_total",
        "Failed MCP tool invocations (any error).",
    )
    metrics.declare_histogram(
        "plinth_mcp_invocation_duration_seconds",
        "MCP tool invocation duration in seconds.",
    )
    app.state.metrics = metrics
    app.middleware("http")(metrics_middleware_factory(metrics))

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "service": "mock-mcp"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request):
        """Prometheus text exposition for the mock-mcp server.

        Public (no auth) by design: Prometheus scrapers don't typically
        authenticate, and the metrics carry no secrets.
        """
        registry: MetricsRegistry = request.app.state.metrics
        return metrics_response(registry)

    @app.get("/tools")
    async def list_tools() -> dict[str, list[dict[str, Any]]]:
        return {"tools": [tool.to_metadata() for tool in TOOL_LIST]}

    @app.post("/invoke/{tool_name:path}")
    async def invoke(tool_name: str, request: Request) -> JSONResponse:
        tool = TOOL_REGISTRY.get(tool_name)
        if tool is None:
            return JSONResponse(
                status_code=404,
                content=_build_error(
                    code="TOOL_NOT_FOUND",
                    message=f"unknown tool: {tool_name}",
                    details={"tool_id": tool_name},
                ),
            )

        # Body parsing — empty body is fine for tools that take no args.
        try:
            raw = await request.body()
            args = await request.json() if raw else {}
        except ValueError:
            return JSONResponse(
                status_code=400,
                content=_build_error(
                    code="INVALID_ARGUMENTS",
                    message="request body is not valid JSON",
                ),
            )

        if not isinstance(args, dict):
            return JSONResponse(
                status_code=400,
                content=_build_error(
                    code="INVALID_ARGUMENTS",
                    message="request body must be a JSON object",
                ),
            )

        ctx = ToolContext(
            fixtures_dir=request.app.state.settings.fixtures_dir,
            notes=request.app.state.notes,
            http_client=request.app.state.http_client,
        )

        log.info("mock-mcp.invoke", tool_id=tool_name)
        registry: MetricsRegistry = request.app.state.metrics
        start_t = time.perf_counter()
        try:
            result = await tool.handler(args, ctx)
        except ToolError as exc:
            log.warning(
                "mock-mcp.invoke.error",
                tool_id=tool_name,
                code=exc.code,
                message=exc.message,
            )
            _record_mcp_invocation(
                registry,
                tool_name,
                start_t,
                ok=False,
            )
            return JSONResponse(
                status_code=exc.status_code,
                content=_build_error(exc.code, exc.message, exc.details),
            )
        except Exception as exc:  # pragma: no cover - safety net
            log.exception("mock-mcp.invoke.unexpected", tool_id=tool_name)
            _record_mcp_invocation(
                registry,
                tool_name,
                start_t,
                ok=False,
            )
            return JSONResponse(
                status_code=500,
                content=_build_error(
                    code="INTERNAL_ERROR",
                    message=f"unexpected error: {exc.__class__.__name__}",
                ),
            )

        _record_mcp_invocation(registry, tool_name, start_t, ok=True)
        return JSONResponse(status_code=200, content={"result": result})

    return app


# Module-level app for `uvicorn mock_mcp.server:app`.
app = create_app()
