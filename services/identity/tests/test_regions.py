# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for identity's ``/v1/regions`` endpoint + region settings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, MockTransport, Response

from plinth_identity.api import create_app
from plinth_identity.regions import RegionStatusProbe
from plinth_identity.settings import Settings


def test_identity_region_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("PLINTH_REGION_ID", "PLINTH_REGION_PEERS", "PLINTH_REPLICATION_MODE"):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.region_id == "default"
    assert s.region_peers == []
    assert s.replication_mode == "standalone"


def test_identity_region_settings_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PLINTH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PLINTH_REGION_ID", "eu")
    monkeypatch.setenv("PLINTH_REGION_PEERS", "us,ap")
    monkeypatch.setenv("PLINTH_REGION_PEER_US_URL", "http://us.example")
    monkeypatch.setenv("PLINTH_REPLICATION_MODE", "replica")

    s = Settings()
    assert s.region_id == "eu"
    assert s.region_peers == ["us", "ap"]
    assert s.region_peer_urls == {"us": "http://us.example"}
    assert s.replication_mode == "replica"


@pytest_asyncio.fixture()
async def identity_regions_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    def peer_handler(request: httpx.Request) -> Response:
        return Response(200, json={"status": "ok"})

    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        region_id="eu-west-1",
        region_peers=["us-east-1"],
        region_peer_urls={"us-east-1": "http://us.example"},
        replication_mode="primary",
    )
    from plinth_identity.store import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    app = create_app(settings)
    mock_client = httpx.AsyncClient(transport=MockTransport(peer_handler))
    app.state.region_status_probe = RegionStatusProbe(
        cache_ttl_seconds=30,
        probe_timeout_seconds=2.0,
        client=mock_client,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_identity_regions_endpoint_shape(
    identity_regions_client: httpx.AsyncClient,
) -> None:
    resp = await identity_regions_client.get("/v1/regions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["current"] == "eu-west-1"
    assert data["mode"] == "primary"
    assert len(data["peers"]) == 1
    assert data["peers"][0]["id"] == "us-east-1"
    assert data["peers"][0]["status"] == "up"


@pytest_asyncio.fixture()
async def identity_replica_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        region_id="us",
        region_peers=["eu"],
        region_peer_urls={"eu": "http://eu.example"},
        replication_mode="replica",
        region_primary_url="http://eu.example",
    )
    from plinth_identity.store import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    app = create_app(settings)
    transport = ASGITransport(app=app)
    # ``lifespan_context`` initialises ``app.state.key_store``,
    # ``app.state.token_manager``, etc. — needed for the verify / JWKS
    # allowlist tests.
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_identity_replica_post_returns_421(
    identity_replica_client: httpx.AsyncClient,
) -> None:
    """A token-mint POST against an identity replica is redirected with 421."""

    resp = await identity_replica_client.post(
        "/v1/tokens",
        json={"tenant_id": "t1", "scopes": ["workspace:r"]},
    )
    assert resp.status_code == 421
    assert resp.headers["X-Plinth-Primary-Region"] == "eu"
    assert resp.headers["X-Plinth-Primary-URL"] == "http://eu.example"


@pytest.mark.asyncio
async def test_identity_replica_allows_verify(
    identity_replica_client: httpx.AsyncClient,
) -> None:
    """``POST /v1/tokens/verify`` is allowlisted (it's read-only)."""

    resp = await identity_replica_client.post(
        "/v1/tokens/verify",
        json={"token": "irrelevant"},
    )
    # Verify will likely 401 / 200 / 400; we only care it isn't 421.
    assert resp.status_code != 421


@pytest.mark.asyncio
async def test_identity_replica_allows_jwks(
    identity_replica_client: httpx.AsyncClient,
) -> None:
    """The JWKS document is available on replicas (GET, allowlisted)."""

    resp = await identity_replica_client.get("/v1/.well-known/jwks.json")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_identity_replica_health_no_auth(
    identity_replica_client: httpx.AsyncClient,
) -> None:
    """``/healthz`` works on a replica without any auth header."""

    resp = await identity_replica_client.get("/healthz")
    assert resp.status_code == 200
