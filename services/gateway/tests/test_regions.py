# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the gateway ``/v1/regions`` endpoint + region settings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, MockTransport, Response

from plinth_gateway.api import create_app
from plinth_gateway.regions import RegionStatusProbe
from plinth_gateway.settings import Settings


def test_gateway_region_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("PLINTH_REGION_ID", "PLINTH_REGION_PEERS", "PLINTH_REPLICATION_MODE"):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.region_id == "default"
    assert s.region_peers == []
    assert s.replication_mode == "standalone"


def test_gateway_region_settings_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PLINTH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PLINTH_REGION_ID", "us-east-1")
    monkeypatch.setenv("PLINTH_REGION_PEERS", "eu-west-1")
    monkeypatch.setenv("PLINTH_REGION_PEER_EU_WEST_1_URL", "http://eu.example")
    s = Settings()
    assert s.region_id == "us-east-1"
    assert s.region_peers == ["eu-west-1"]
    assert s.region_peer_urls == {"eu-west-1": "http://eu.example"}


@pytest_asyncio.fixture
async def gateway_regions_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    def peer_handler(request: httpx.Request) -> Response:
        return Response(200, json={"status": "ok"})

    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        inbound_auth_required=False,
        region_id="eu-west-1",
        region_peers=["us-east-1"],
        region_peer_urls={"us-east-1": "http://us.example"},
        replication_mode="primary",
    )
    settings.ensure_data_dir()
    app = create_app(settings)
    mock_client = httpx.AsyncClient(transport=MockTransport(peer_handler))
    app.state.region_status_probe = RegionStatusProbe(
        cache_ttl_seconds=30,
        probe_timeout_seconds=2.0,
        client=mock_client,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        async with app.router.lifespan_context(app):
            yield client


@pytest.mark.asyncio
async def test_gateway_regions_endpoint_shape(
    gateway_regions_client: httpx.AsyncClient,
) -> None:
    resp = await gateway_regions_client.get("/v1/regions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["current"] == "eu-west-1"
    assert data["mode"] == "primary"
    assert len(data["peers"]) == 1
    assert data["peers"][0]["id"] == "us-east-1"


@pytest.mark.asyncio
async def test_gateway_regions_endpoint_no_peers(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        inbound_auth_required=False,
        region_id="solo",
    )
    settings.ensure_data_dir()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        async with app.router.lifespan_context(app):
            resp = await client.get("/v1/regions")
            assert resp.status_code == 200
            data = resp.json()
            assert data["current"] == "solo"
            assert data["peers"] == []


@pytest_asyncio.fixture
async def gateway_replica_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        inbound_auth_required=False,
        region_id="us",
        region_peers=["eu"],
        region_peer_urls={"eu": "http://eu.example"},
        replication_mode="replica",
        region_primary_url="http://eu.example",
    )
    settings.ensure_data_dir()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        async with app.router.lifespan_context(app):
            yield client


@pytest.mark.asyncio
async def test_gateway_replica_post_returns_421(
    gateway_replica_client: httpx.AsyncClient,
) -> None:
    """The gateway's replica middleware emits 421 + both redirect headers."""

    resp = await gateway_replica_client.post(
        "/v1/invoke",
        json={"tool_id": "noop", "arguments": {}},
    )
    assert resp.status_code == 421
    assert resp.headers["X-Plinth-Primary-Region"] == "eu"
    assert resp.headers["X-Plinth-Primary-URL"] == "http://eu.example"
    assert resp.headers["Location"].startswith("http://eu.example")
    error = resp.json()["error"]
    assert error["code"] == "REPLICA_READ_ONLY"


@pytest.mark.asyncio
async def test_gateway_replica_allows_regions_endpoint(
    gateway_replica_client: httpx.AsyncClient,
) -> None:
    """`/v1/regions` is reachable on a replica even via GET."""

    resp = await gateway_replica_client.get("/v1/regions")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "replica"


@pytest.mark.asyncio
async def test_gateway_replica_allows_dry_run(
    gateway_replica_client: httpx.AsyncClient,
) -> None:
    """The dry-run endpoint is allowlisted on replicas (it's read-only)."""

    resp = await gateway_replica_client.post(
        "/v1/invoke/dry-run",
        json={"tool_id": "noop", "arguments": {}},
    )
    # We only care that we DIDN'T get the 421 redirect — the actual
    # response may be a 4xx for a missing tool, etc.
    assert resp.status_code != 421
