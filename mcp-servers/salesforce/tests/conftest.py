# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared fixtures for the salesforce-mcp tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from salesforce_mcp.server import create_app
from salesforce_mcp.settings import Settings


# Test instance URL on a permitted ``.salesforce.test`` host (registered in
# the default ``allowed_host_suffixes`` set). Re-exported by tests/test_tools.py.
INSTANCE_URL = "https://acme.my.salesforce.test"
API_VERSION = "v60.0"


@pytest.fixture
def settings() -> Settings:
    """Test-only settings: short timeouts."""
    return Settings(
        port=0,
        api_version=API_VERSION,
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
