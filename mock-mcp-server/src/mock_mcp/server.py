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

from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .logging_config import configure_logging, get_logger
from .settings import Settings, get_settings
from .tools import TOOL_LIST, TOOL_REGISTRY, ToolContext, ToolError


def _build_error(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the canonical Plinth error envelope."""
    return {"error": {"code": code, "message": message, "details": details or {}}}


def create_app(settings: Settings | None = None) -> FastAPI:
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

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "service": "mock-mcp"}

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
        try:
            result = await tool.handler(args, ctx)
        except ToolError as exc:
            log.warning(
                "mock-mcp.invoke.error",
                tool_id=tool_name,
                code=exc.code,
                message=exc.message,
            )
            return JSONResponse(
                status_code=exc.status_code,
                content=_build_error(exc.code, exc.message, exc.details),
            )
        except Exception as exc:  # pragma: no cover - safety net
            log.exception("mock-mcp.invoke.unexpected", tool_id=tool_name)
            return JSONResponse(
                status_code=500,
                content=_build_error(
                    code="INTERNAL_ERROR",
                    message=f"unexpected error: {exc.__class__.__name__}",
                ),
            )

        return JSONResponse(status_code=200, content={"result": result})

    return app


# Module-level app for `uvicorn mock_mcp.server:app`.
app = create_app()
