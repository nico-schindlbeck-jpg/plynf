# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared fixtures for the atlassian-mcp tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from atlassian_mcp.server import create_app
from atlassian_mcp.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """Test-only settings: short timeouts, mock Atlassian host."""
    return Settings(
        port=0,
        api_base_url="https://api.atlassian.test",
        request_timeout_seconds=2.0,
        log_level="WARNING",
        log_format="console",
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


# A canonical Atlassian cloudid we'll reuse in fixtures.
CLOUDID = "abc-123-cloudid"
