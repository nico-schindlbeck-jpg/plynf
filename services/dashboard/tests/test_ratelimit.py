# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Tests for the auth-surface rate limiter."""

from __future__ import annotations

from fastapi.testclient import TestClient

from plinth_dashboard.ratelimit import SlidingWindowLimiter
from plinth_dashboard.server import create_app
from plinth_dashboard.settings import Settings


def test_sliding_window_allows_then_blocks_then_recovers():
    lim = SlidingWindowLimiter(limit=3, window_s=10.0)
    assert lim.allow("ip", now=0.0)
    assert lim.allow("ip", now=1.0)
    assert lim.allow("ip", now=2.0)
    assert not lim.allow("ip", now=3.0)
    assert lim.retry_after_s("ip", now=3.0) >= 1
    # Window slides: the first event ages out at t=10.
    assert lim.allow("ip", now=10.5)


def test_keys_are_independent():
    lim = SlidingWindowLimiter(limit=1, window_s=60.0)
    assert lim.allow("a", now=0.0)
    assert lim.allow("b", now=0.0)
    assert not lim.allow("a", now=1.0)


def test_zero_limit_disables():
    lim = SlidingWindowLimiter(limit=0, window_s=60.0)
    for i in range(100):
        assert lim.allow("ip", now=float(i))


def test_login_returns_429_after_limit():
    settings = Settings(auth_rate_limit_login=3, auth_rate_window_s=60.0)
    client = TestClient(create_app(settings))
    body = {"email": "nobody@example.com", "password": "wrong-password"}
    for _ in range(3):
        resp = client.post("/api/app/login", json=body)
        assert resp.status_code == 401  # wrong creds, but not rate-limited yet
    resp = client.post("/api/app/login", json=body)
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") is not None
    assert resp.json()["error"]["code"] == "RATE_LIMITED"


def test_signup_returns_429_after_limit():
    settings = Settings(auth_rate_limit_signup=2, auth_rate_window_s=60.0)
    client = TestClient(create_app(settings))
    for i in range(2):
        resp = client.post(
            "/api/app/signup",
            json={"email": f"u{i}@example.com", "password": "long-enough-pw-12"},
        )
        assert resp.status_code == 200
    resp = client.post(
        "/api/app/signup",
        json={"email": "u3@example.com", "password": "long-enough-pw-12"},
    )
    assert resp.status_code == 429
