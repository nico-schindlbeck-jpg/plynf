# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared pytest fixtures for the gateway tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from plinth_gateway.api import create_app
from plinth_gateway.db import Database
from plinth_gateway.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Per-test settings rooted at a fresh tmp dir."""
    return Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
    )


@pytest_asyncio.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    """Direct database fixture — for unit tests below the API layer."""
    settings.ensure_data_dir()
    database = Database(settings.db_path)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest_asyncio.fixture
async def app_and_client(
    settings: Settings,
) -> AsyncIterator[tuple]:
    """FastAPI app + AsyncClient pre-wired with the lifespan."""
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        # Trigger lifespan startup
        async with app.router.lifespan_context(app):
            yield app, client


@pytest_asyncio.fixture
async def client(app_and_client) -> AsyncClient:
    """Just the client (when tests don't need the app handle)."""
    _, c = app_and_client
    return c


def sample_tool(
    *,
    tool_id: str = "web.fetch",
    endpoint: str = "http://mcp.test/invoke/fetch",
    idempotent: bool = True,
    cache_ttl_seconds: int | None = 300,
    auth_method: str = "none",
    auth_config: dict | None = None,
) -> dict:
    """Helper: build a ToolRegistration body."""
    return {
        "tool_id": tool_id,
        "name": tool_id.replace(".", " ").title(),
        "description": f"Mock tool {tool_id}",
        "transport": "http",
        "endpoint": endpoint,
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "idempotent": idempotent,
        "side_effects": "read",
        "cache_ttl_seconds": cache_ttl_seconds,
        "auth_method": auth_method,
        "auth_config": auth_config or {},
    }


@pytest.fixture
def make_tool():
    """Factory fixture returning :func:`sample_tool`."""
    return sample_tool
