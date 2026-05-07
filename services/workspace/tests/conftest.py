# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared pytest fixtures for the workspace service test suite.

Every test gets a fresh ``tmp_path``-backed data dir, so SQLite + blobs are
isolated. The FastAPI app is rebuilt per-test, with lifespan triggered via
``httpx.AsyncClient`` + ``ASGITransport``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from plinth_workspace.api import create_app
from plinth_workspace.settings import Settings
from plinth_workspace.snapshots import SnapshotStore
from plinth_workspace.storage import WorkspaceStore

AUTH_HEADER = {"Authorization": "Bearer test-token"}


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plinth-data"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        workspace_port=17421,
        workspace_host="127.0.0.1",
        log_level="WARNING",
        log_format="console",
        auth_required=False,
    )


@pytest_asyncio.fixture()
async def store(settings: Settings) -> WorkspaceStore:
    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    return WorkspaceStore(settings.db_path, settings.blobs_dir)


@pytest_asyncio.fixture()
async def snapshots(store: WorkspaceStore) -> SnapshotStore:
    return SnapshotStore(store)


@pytest_asyncio.fixture()
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    # ASGITransport does not run lifespan events on its own, so initialise
    # the DB up-front to mirror what lifespan would do in production.
    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=AUTH_HEADER,
    ) as c:
        yield c


@pytest_asyncio.fixture()
async def workspace_id(client: httpx.AsyncClient) -> str:
    resp = await client.post("/v1/workspaces", json={"name": "test-ws"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]
