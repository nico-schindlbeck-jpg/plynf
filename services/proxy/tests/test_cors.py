# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""CORS — lets the install-page 'Test connection' check hit the proxy from a
browser on the marketing origin. Restricted to the configured allow-list."""

from __future__ import annotations

from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings


def _client(origins: str = "https://plynf.com") -> TestClient:
    return TestClient(create_app(ProxySettings(demo_mode=True, cors_origins=origins)))


def test_cors_allows_configured_origin():
    r = _client().get("/healthz", headers={"Origin": "https://plynf.com"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://plynf.com"


def test_cors_preflight_for_tier_with_auth_header():
    r = _client().options(
        "/v1/tier",
        headers={
            "Origin": "https://plynf.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "https://plynf.com"


def test_cors_does_not_echo_unknown_origin():
    r = _client().get("/healthz", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200  # request still served
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"


def test_cors_disabled_when_origins_empty():
    r = _client(origins="").get("/healthz", headers={"Origin": "https://plynf.com"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers
