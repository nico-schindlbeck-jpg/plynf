# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""FastAPI app for the Atlassian MCP server.

Exposes:

* ``GET /healthz`` — liveness/version probe.
* ``GET /tools`` — full tool registry metadata.
* ``POST /invoke/{tool_name}`` — dispatch a tool call. Reads the access token
  from the inbound ``Authorization: Bearer ...`` header AND the workspace
  cloudid from ``X-Plinth-OAuth-Cloudid`` (gateway populates both).
* ``GET /metrics`` — Prometheus exposition.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request
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
    return {"error": {"code": code, "message": message, "details": details or {}}}


def _record_mcp_invocation(
    registry: MetricsRegistry | None,
    tool: str,
    start: float,
    *,
    ok: bool,
) -> None:
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
    except Exception:  # noqa: BLE001
        pass


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a FastAPI app for the Atlassian MCP server."""
    if settings is None:
        settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    log = get_logger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient() as client:
            app.state.http_client = client
            app.state.settings = settings
            log.info(
                "atlassian-mcp.startup",
                port=settings.port,
                api_base_url=settings.api_base_url,
                tools=len(TOOL_LIST),
            )
            yield
        log.info("atlassian-mcp.shutdown")

    app = FastAPI(
        title="Plinth Atlassian MCP Server",
        version=__version__,
        lifespan=lifespan,
    )

    metrics = MetricsRegistry(service_name="atlassian-mcp", version=__version__)
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

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "service": "atlassian-mcp"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request):
        registry: MetricsRegistry = request.app.state.metrics
        return metrics_response(registry)

    @app.get("/tools")
    async def list_tools() -> dict[str, list[dict[str, Any]]]:
        return {"tools": [tool.to_metadata() for tool in TOOL_LIST]}

    @app.post("/invoke/{tool_name:path}")
    async def invoke(
        tool_name: str,
        request: Request,
        authorization: str | None = Header(default=None),
        x_plinth_oauth_cloudid: str | None = Header(default=None),
    ) -> JSONResponse:
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

        token = _extract_bearer(authorization)
        cloudid = (x_plinth_oauth_cloudid or "").strip() or None
        ctx = ToolContext(
            access_token=token,
            cloudid=cloudid,
            http_client=request.app.state.http_client,
            api_base_url=request.app.state.settings.api_base_url,
            timeout=request.app.state.settings.request_timeout_seconds,
        )

        log.info(
            "atlassian-mcp.invoke",
            tool_id=tool_name,
            has_token=bool(token),
            has_cloudid=bool(cloudid),
        )
        registry: MetricsRegistry = request.app.state.metrics
        start_t = time.perf_counter()
        try:
            result = await tool.handler(args, ctx)
        except ToolError as exc:
            log.warning(
                "atlassian-mcp.invoke.error",
                tool_id=tool_name,
                code=exc.code,
                message=exc.message,
            )
            _record_mcp_invocation(registry, tool_name, start_t, ok=False)
            return JSONResponse(
                status_code=exc.status_code,
                content=_build_error(exc.code, exc.message, exc.details),
            )
        except Exception as exc:  # pragma: no cover
            log.exception("atlassian-mcp.invoke.unexpected", tool_id=tool_name)
            _record_mcp_invocation(registry, tool_name, start_t, ok=False)
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


app = create_app()
