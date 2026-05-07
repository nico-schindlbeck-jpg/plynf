# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared pytest fixtures for the identity service test suite.

Each test gets a fresh ``tmp_path``-backed data dir so SQLite stays isolated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from plinth_identity.api import create_app
from plinth_identity.jwt_io import TokenManager
from plinth_identity.settings import Settings
from plinth_identity.store import TokenStore, init_db

# A deterministic test secret. 44 chars is the base64 encoding of 32 bytes,
# so it satisfies the HS256 minimum-length recommendation.
TEST_SECRET = "test-secret-44chars-for-hs256-cZbR3lKp9q8WxYzAAA="


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plinth-data"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        identity_port=17425,
        identity_host="127.0.0.1",
        identity_url="http://identity.test",
        identity_jwt_secret=TEST_SECRET,
        identity_jwt_audience="plinth",
        log_level="WARNING",
        log_format="console",
    )


@pytest.fixture()
def token_manager(settings: Settings) -> TokenManager:
    return TokenManager(
        secret=settings.resolve_secret(),
        issuer=settings.identity_url,
        audience=settings.identity_jwt_audience,
    )


@pytest_asyncio.fixture()
async def store(settings: Settings) -> TokenStore:
    await init_db(settings.db_path)
    return TokenStore(settings.db_path)


@pytest_asyncio.fixture()
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """A httpx AsyncClient pre-wired with the in-process FastAPI app + lifespan."""

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as c, app.router.lifespan_context(app):
        yield c
