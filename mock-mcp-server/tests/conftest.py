# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared pytest fixtures for the mock-mcp test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from mock_mcp.server import create_app
from mock_mcp.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointing at an isolated, empty fixtures directory."""
    return Settings(
        port=0,
        fixtures_dir=tmp_path / "fixtures",
        log_level="WARNING",
        log_format="console",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """A fresh FastAPI app instance configured for the test."""
    return create_app(settings=settings)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An ``httpx.AsyncClient`` wired to the app via ASGITransport.

    The lifespan context manager runs so startup/shutdown logic fires.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        async with app.router.lifespan_context(app):
            yield client
