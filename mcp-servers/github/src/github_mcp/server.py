# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""FastAPI app for the GitHub MCP server.

Exposes:

* ``GET /healthz`` — liveness/version probe.
* ``GET /tools`` — full tool registry metadata.
* ``POST /invoke/{tool_name}`` — dispatch a tool call. Reads the GitHub access
  token from the inbound ``Authorization: Bearer ...`` header (forwarded by
  the gateway).

This server is intentionally thin: it parses the auth header, dispatches to
:mod:`github_mcp.tools`, and surfaces errors via the canonical Plinth error
envelope. All input validation and HTTP plumbing live in the tools module.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from . import __version__
from .logging_config import configure_logging, get_logger
from .settings import Settings, get_settings
from .tools import TOOL_LIST, TOOL_REGISTRY, ToolContext, ToolError


def _build_error(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the canonical Plinth error envelope."""
    return {"error": {"code": code, "message": message, "details": details or {}}}


def _extract_bearer(authorization: str | None) -> str | None:
    """Return the bearer token from an ``Authorization`` header, or None."""
    if not authorization:
        return None
    parts = authorization.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a FastAPI app for the GitHub MCP server.

    Args:
        settings: Optional explicit settings (used by tests). Falls back to
            :func:`get_settings`, which reads environment variables.
    """
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
                "github-mcp.startup",
                port=settings.port,
                api_base_url=settings.api_base_url,
                tools=len(TOOL_LIST),
            )
            yield
        log.info("github-mcp.shutdown")

    app = FastAPI(
        title="Plinth GitHub MCP Server",
        version=__version__,
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "service": "github-mcp"}

    @app.get("/tools")
    async def list_tools() -> dict[str, list[dict[str, Any]]]:
        return {"tools": [tool.to_metadata() for tool in TOOL_LIST]}

    @app.post("/invoke/{tool_name:path}")
    async def invoke(
        tool_name: str,
        request: Request,
        authorization: str | None = Header(default=None),
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

        # Body parsing — empty body is OK for tools that take no args.
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
        ctx = ToolContext(
            access_token=token,
            http_client=request.app.state.http_client,
            api_base_url=request.app.state.settings.api_base_url,
            api_version=request.app.state.settings.api_version,
            timeout=request.app.state.settings.request_timeout_seconds,
        )

        log.info("github-mcp.invoke", tool_id=tool_name, has_token=bool(token))
        try:
            result = await tool.handler(args, ctx)
        except ToolError as exc:
            log.warning(
                "github-mcp.invoke.error",
                tool_id=tool_name,
                code=exc.code,
                message=exc.message,
            )
            return JSONResponse(
                status_code=exc.status_code,
                content=_build_error(exc.code, exc.message, exc.details),
            )
        except Exception as exc:  # pragma: no cover - safety net
            log.exception("github-mcp.invoke.unexpected", tool_id=tool_name)
            return JSONResponse(
                status_code=500,
                content=_build_error(
                    code="INTERNAL_ERROR",
                    message=f"unexpected error: {exc.__class__.__name__}",
                ),
            )
        return JSONResponse(status_code=200, content={"result": result})

    return app


app = create_app()
