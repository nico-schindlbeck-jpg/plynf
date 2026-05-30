# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``/readyz`` readiness probe (distinct from ``/healthz``)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def test_readyz_ready_when_state_built(demo_client):
    r = demo_client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["ready"] is True
    assert "version" in body
    assert body["checks"]["connectors"] == "ok"
    assert body["checks"]["policies_loaded"] >= 0


def test_readyz_reports_open_mode_when_no_identity(demo_client):
    # Demo mode has no identity service configured → open-mode auth.
    checks = demo_client.get("/readyz").json()["checks"]
    assert checks["identity"] == "open-mode"
    # Demo mode uses the mock connector registry, not a live gateway.
    assert checks["gateway"] == "mock"
    assert checks["savings_sink"] == "none"


def test_readyz_503_before_initialization():
    # Simulate a probe hitting the app before lifespan finishes wiring state.
    app = create_app(ProxySettings(demo_mode=True))
    del app.state.plinth
    # No `with` block → lifespan startup does not run, so state stays absent.
    client = TestClient(app)
    r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert body["status"] == "initializing"


def test_healthz_distinct_from_readyz(demo_client):
    # Liveness still reports ok and carries its own shape.
    h = demo_client.get("/healthz").json()
    assert h["status"] == "ok"
    assert "demo_mode" in h
