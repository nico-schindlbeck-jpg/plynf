# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the workspace ``/v1/regions`` endpoint + region settings.

These cover env-var parsing for the v1.0 multi-region knobs and the
peer-status probe behaviour exposed via ``GET /v1/regions``.
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, MockTransport, Response

from plinth_workspace.api import create_app
from plinth_workspace.regions import RegionStatusProbe
from plinth_workspace.settings import Settings


AUTH_HEADER = {"Authorization": "Bearer test-token"}


# ---------------------------------------------------------------------------
# Settings parsing


def test_region_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # No env vars set → all defaults.
    for var in (
        "PLINTH_REGION_ID",
        "PLINTH_REGION_PEERS",
        "PLINTH_REPLICATION_MODE",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.region_id == "default"
    assert s.region_peers == []
    assert s.replication_mode == "standalone"
    assert s.region_peer_urls == {}


def test_region_settings_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PLINTH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PLINTH_REGION_ID", "eu-west-1")
    monkeypatch.setenv("PLINTH_REGION_PEERS", "us-east-1,ap-south-1")
    monkeypatch.setenv("PLINTH_REGION_PEER_US_EAST_1_URL", "https://us.plinth.example")
    monkeypatch.setenv("PLINTH_REGION_PEER_AP_SOUTH_1_URL", "https://ap.plinth.example")
    monkeypatch.setenv("PLINTH_REPLICATION_MODE", "primary")

    s = Settings()
    assert s.region_id == "eu-west-1"
    assert s.region_peers == ["us-east-1", "ap-south-1"]
    assert s.replication_mode == "primary"
    assert s.region_peer_urls == {
        "us-east-1": "https://us.plinth.example",
        "ap-south-1": "https://ap.plinth.example",
    }


def test_region_settings_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLINTH_REGION_PEERS", "from-env")
    s = Settings(region_peers=["explicit-1", "explicit-2"])
    # Explicit constructor args win — same pattern as other settings.
    assert s.region_peers == ["explicit-1", "explicit-2"]


def test_region_settings_invalid_mode_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLINTH_REPLICATION_MODE", "leader")
    with pytest.raises(Exception):
        Settings()


def test_region_settings_empty_peers_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLINTH_REGION_PEERS", "")
    s = Settings()
    assert s.region_peers == []


# ---------------------------------------------------------------------------
# /v1/regions endpoint


@pytest_asyncio.fixture()
async def regions_client(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Settings]]:
    """A client where two peers are configured + reachable via MockTransport."""

    def peer_handler(request: httpx.Request) -> Response:
        if request.url.path == "/healthz":
            return Response(200, json={"status": "ok"})
        return Response(404)

    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        region_id="eu-west-1",
        region_peers=["us-east-1", "ap-south-1"],
        region_peer_urls={
            "us-east-1": "http://us.example",
            "ap-south-1": "http://ap.example",
        },
        replication_mode="primary",
    )

    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)

    app = create_app(settings)
    # Override the probe's httpx client with a mock so we don't hit the
    # network during tests.
    mock_client = httpx.AsyncClient(transport=MockTransport(peer_handler))
    app.state.region_status_probe = RegionStatusProbe(
        cache_ttl_seconds=settings.regions_status_cache_ttl_seconds,
        probe_timeout_seconds=settings.regions_status_probe_timeout_seconds,
        client=mock_client,
    )

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=AUTH_HEADER,
    ) as client:
        yield client, settings


@pytest.mark.asyncio
async def test_regions_endpoint_shape(
    regions_client: tuple[httpx.AsyncClient, Settings],
) -> None:
    client, settings = regions_client
    resp = await client.get("/v1/regions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["current"] == "eu-west-1"
    assert data["mode"] == "primary"
    assert {p["id"] for p in data["peers"]} == {"us-east-1", "ap-south-1"}
    for peer in data["peers"]:
        assert peer["status"] == "up"
        assert peer["lag_ms"] is not None
        assert peer["last_seen_at"] is not None


@pytest.mark.asyncio
async def test_regions_endpoint_no_peers(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, region_id="solo", log_level="WARNING")
    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADER
    ) as client:
        resp = await client.get("/v1/regions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["current"] == "solo"
    assert data["mode"] == "standalone"
    assert data["peers"] == []


@pytest.mark.asyncio
async def test_regions_endpoint_peer_down(tmp_path: Path) -> None:
    """A peer that returns 5xx is reported as ``down``."""

    def peer_handler(request: httpx.Request) -> Response:
        return Response(503)

    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        region_id="eu",
        region_peers=["us"],
        region_peer_urls={"us": "http://us.example"},
        replication_mode="primary",
    )
    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
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
        transport=transport, base_url="http://test", headers=AUTH_HEADER
    ) as client:
        resp = await client.get("/v1/regions")
    data = resp.json()
    assert data["peers"][0]["status"] == "down"


@pytest.mark.asyncio
async def test_regions_status_cached(tmp_path: Path) -> None:
    """Two consecutive calls share a single peer probe."""

    call_count = {"n": 0}

    def peer_handler(request: httpx.Request) -> Response:
        call_count["n"] += 1
        return Response(200, json={"status": "ok"})

    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        region_id="eu",
        region_peers=["us"],
        region_peer_urls={"us": "http://us.example"},
        replication_mode="primary",
        regions_status_cache_ttl_seconds=60,
    )
    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    app = create_app(settings)
    mock_client = httpx.AsyncClient(transport=MockTransport(peer_handler))
    app.state.region_status_probe = RegionStatusProbe(
        cache_ttl_seconds=60,
        probe_timeout_seconds=2.0,
        client=mock_client,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADER
    ) as client:
        await client.get("/v1/regions")
        await client.get("/v1/regions")
    # The probe is cached — second call shouldn't trigger a fresh ping.
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Replica redirect headers (421 + X-Plinth-Primary-URL)


@pytest_asyncio.fixture()
async def workspace_replica_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """A client where the local instance is a read-replica."""

    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        region_id="us-east-1",
        region_peers=["eu-west-1"],
        region_peer_urls={"eu-west-1": "http://eu.example"},
        replication_mode="replica",
        region_primary_url="http://eu.example",
    )
    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    app = create_app(settings)
    await app.state.replication_log.init()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADER
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_replica_post_returns_421_with_both_headers(
    workspace_replica_client: httpx.AsyncClient,
) -> None:
    """The 421 response carries every header the SDK needs to retry."""

    resp = await workspace_replica_client.post(
        "/v1/workspaces",
        json={"name": "ws1"},
    )
    assert resp.status_code == 421
    assert resp.headers["X-Plinth-Primary-Region"] == "eu-west-1"
    assert resp.headers["X-Plinth-Primary-URL"] == "http://eu.example"
    assert resp.headers["Location"] == "http://eu.example/v1/workspaces"
    error = resp.json()["error"]
    assert error["code"] == "REPLICA_READ_ONLY"
    assert error["details"]["primary_region"] == "eu-west-1"


@pytest.mark.asyncio
async def test_replica_get_works_unauthenticated(tmp_path: Path) -> None:
    """``/healthz`` and ``/v1/regions`` are reachable on a replica without auth."""

    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        region_id="us",
        region_peers=["eu"],
        region_peer_urls={"eu": "http://eu.example"},
        replication_mode="replica",
        region_primary_url="http://eu.example",
    )
    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    app = create_app(settings)
    await app.state.replication_log.init()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        # No Authorization header.
        health = await client.get("/healthz")
        assert health.status_code == 200
        regions = await client.get("/v1/regions")
        assert regions.status_code == 200


@pytest.mark.asyncio
async def test_replica_allows_get(
    workspace_replica_client: httpx.AsyncClient,
) -> None:
    """A replica accepts read traffic — only mutating verbs are redirected."""

    resp = await workspace_replica_client.get("/v1/workspaces")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_replica_redirect_excludes_replication_apply(
    workspace_replica_client: httpx.AsyncClient,
) -> None:
    """The replica's apply endpoint is allowlisted (otherwise replication can't work)."""

    resp = await workspace_replica_client.post(
        "/v1/admin/replication/apply",
        json=[],
    )
    # Either 200/201 (accepted, even if empty) or a non-redirect status —
    # we only care that the middleware DID NOT short-circuit with 421.
    assert resp.status_code != 421


@pytest.mark.asyncio
async def test_replica_redirect_url_omitted_without_primary_url(tmp_path: Path) -> None:
    """When no primary URL is configured, the X-Plinth-Primary-URL header is absent."""

    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        region_id="us",
        region_peers=["eu"],
        # No URL for ``eu`` and no ``region_primary_url`` either.
        region_peer_urls={},
        replication_mode="replica",
    )
    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    app = create_app(settings)
    await app.state.replication_log.init()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADER
    ) as client:
        resp = await client.post("/v1/workspaces", json={"name": "x"})
    assert resp.status_code == 421
    assert resp.headers["X-Plinth-Primary-Region"] == "eu"
    # No URL configured → header is omitted (the SDK should fall back to
    # the region-id lookup against its own configuration).
    assert "X-Plinth-Primary-URL" not in resp.headers
    assert "Location" not in resp.headers
