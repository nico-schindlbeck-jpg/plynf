# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the identity metrics module + /metrics endpoint."""

from __future__ import annotations

import httpx
import pytest

from plinth_identity.metrics import MetricsRegistry


def test_registry_canonical_identity_series():
    r = MetricsRegistry("identity", "1.0.0")
    r.declare_counter("plinth_tokens_issued_total", "test")
    r.declare_counter("plinth_tokens_revoked_total", "test")
    r.declare_gauge("plinth_tokens_active", "test")
    r.declare_counter("plinth_token_verifications_total", "test")
    text = r.render()
    for name in (
        "plinth_tokens_issued_total",
        "plinth_tokens_revoked_total",
        "plinth_tokens_active",
        "plinth_token_verifications_total",
    ):
        assert f"# TYPE {name}" in text


@pytest.mark.asyncio
async def test_identity_metrics_endpoint(client: httpx.AsyncClient):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "plinth_build_info" in body
    assert 'service="identity"' in body
    # Pre-declared identity-specific series.
    assert "# TYPE plinth_tokens_issued_total" in body
    assert "# TYPE plinth_token_verifications_total" in body


@pytest.mark.asyncio
async def test_identity_metrics_records_http_request(client: httpx.AsyncClient):
    await client.get("/healthz")  # excluded
    await client.get("/v1/.well-known/jwks.json")  # included
    resp = await client.get("/metrics")
    body = resp.text
    # JWKS counts; healthz does not.
    assert 'path="/healthz"' not in body
    assert 'path="/v1/.well-known/jwks.json"' in body
