# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared pytest fixtures for the dashboard tests.

Each test gets a fresh :class:`Settings` pointing at fake URLs, a shared
``httpx.AsyncClient`` (so ``respx`` can mock the calls), and a FastAPI
``AsyncClient`` wired against the in-process app.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from plinth_dashboard.overview import OverviewBuilder
from plinth_dashboard.server import create_app
from plinth_dashboard.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """Per-test settings — points at fake URLs we mock with respx."""
    return Settings(
        port=7424,
        host="127.0.0.1",
        workspace_url="http://workspace.test",
        gateway_url="http://gateway.test",
        mock_mcp_url="http://mock-mcp.test",
        identity_url="http://identity.test",
        api_token="Bearer test",
        log_level="WARNING",
        log_format="console",
    )


@pytest_asyncio.fixture
async def http_client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """A fresh ``httpx.AsyncClient`` we hand to the OverviewBuilder."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.backend_timeout_seconds),
        headers={"Authorization": settings.auth_header},
    ) as client:
        yield client


@pytest_asyncio.fixture
async def overview(
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> OverviewBuilder:
    """An :class:`OverviewBuilder` bound to the injected httpx client."""
    return OverviewBuilder(settings, client=http_client)


@pytest_asyncio.fixture
async def app_and_client(
    settings: Settings,
    overview: OverviewBuilder,
) -> AsyncIterator[tuple]:
    """FastAPI app + AsyncClient pre-wired with the lifespan and injected builder."""
    app = create_app(settings, overview=overview)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client, app.router.lifespan_context(app):
        yield app, client


@pytest_asyncio.fixture
async def client(app_and_client) -> AsyncClient:
    _, c = app_and_client
    return c


# ---------------------------------------------------------------------------
# Backend response factories — small, opinionated builders for fixtures.


def make_workspace(ws_id: str = "ws_a", name: str = "research-1", **kwargs) -> dict:
    base = {
        "id": ws_id,
        "name": name,
        "created_at": "2026-05-05T00:00:00Z",
        "updated_at": "2026-05-05T01:00:00Z",
        "metadata": {},
    }
    base.update(kwargs)
    return base


def make_audit_stats(
    *,
    total_invocations: int = 142,
    cached_count: int = 38,
    error_count: int = 0,
    total_cost_usd: float = 0.0234,
    by_tool: list[dict] | None = None,
) -> dict:
    return {
        "stats": {
            "total_invocations": total_invocations,
            "cached_count": cached_count,
            "error_count": error_count,
            "total_cost_usd": total_cost_usd,
            "by_tool": by_tool
            or [
                {"tool_id": "web.fetch", "count": 80, "cost": 0.0140},
                {"tool_id": "web.search", "count": 40, "cost": 0.0070},
                {"tool_id": "notes.add", "count": 22, "cost": 0.0024},
            ],
        }
    }


@pytest.fixture
def workspace_factory():
    return make_workspace


@pytest.fixture
def audit_stats_factory():
    return make_audit_stats
