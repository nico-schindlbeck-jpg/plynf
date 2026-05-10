# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared fixtures for the notion-mcp tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from notion_mcp.server import create_app
from notion_mcp.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """Test-only settings: short timeouts, mock Notion host."""
    return Settings(
        port=0,
        api_base_url="https://api.notion.test",
        request_timeout_seconds=2.0,
        log_level="WARNING",
        log_format="console",
        api_version="2022-06-28",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings=settings)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        async with app.router.lifespan_context(app):
            yield client
