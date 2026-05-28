# SPDX-License-Identifier: Apache-2.0
"""Tests for the tier-gate middleware."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings
from plinth_proxy.tier_gate import TIERS, TierGate, upgrade_hint

FIXTURES = Path(__file__).resolve().parent.parent.parent.parent / "examples" / "customer-support"


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_tiers_have_expected_volume_caps():
    assert TIERS["free"].monthly_token_budget == 100_000
    assert TIERS["pro"].monthly_token_budget == 5_000_000
    assert TIERS["enterprise"].monthly_token_budget is None


def test_gate_blocks_free_after_budget():
    g = TierGate()
    g.record_tokens("tenant-1", 99_999)
    ok, _ = g.check("tenant-1", "free")
    assert ok is True
    g.record_tokens("tenant-1", 100)
    ok, reason = g.check("tenant-1", "free")
    assert ok is False
    assert reason == "monthly_token_budget_exceeded"


def test_gate_allows_pro_at_free_threshold():
    g = TierGate()
    g.record_tokens("tenant-1", 200_000)
    ok, _ = g.check("tenant-1", "pro")
    assert ok is True


def test_gate_enterprise_never_blocks_on_volume():
    g = TierGate()
    g.record_tokens("tenant-1", 10_000_000_000)  # 10B tokens
    ok, _ = g.check("tenant-1", "enterprise")
    assert ok is True


def test_gate_rejects_unknown_tier():
    g = TierGate()
    ok, reason = g.check("tenant-1", "platinum")  # type: ignore[arg-type]
    assert ok is False
    assert reason.startswith("unknown_tier")


def test_upgrade_hint_messaging():
    assert "Pro" in upgrade_hint("free")
    assert "Enterprise" in upgrade_hint("pro")
    assert "Enterprise" in upgrade_hint("enterprise")


# ---------------------------------------------------------------------------
# End-to-end via the API
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_free_keyed():
    # Configure a free-tier API key bound to one tenant.
    settings = ProxySettings(
        demo_mode=True,
        api_keys="tenant-a:key-a:free",
    )
    return create_app(settings)


def test_tier_endpoint_reports_current_tier(app_with_free_keyed):
    client = TestClient(app_with_free_keyed)
    r = client.get("/v1/tier", headers={"Authorization": "Bearer key-a"})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["tier"] == "free"


def test_chat_endpoint_returns_402_when_free_budget_exceeded(app_with_free_keyed):
    client = TestClient(app_with_free_keyed)
    # Pre-charge the gate to exhaust the free tier.
    state = app_with_free_keyed.state.plinth
    state.gate.record_tokens("tenant-a", 200_000)

    body = json.loads((FIXTURES / "demo_request.json").read_text(encoding="utf-8"))
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer key-a"},
        json=body,
    )
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["reason"] == "monthly_token_budget_exceeded"
    assert detail["tier"] == "free"
    assert "Upgrade to Pro" in detail["upgrade_hint"]


def test_pro_tier_passes_when_free_would_block(app_with_free_keyed):
    # Add a pro key, exhaust the free budget — pro should still work.
    settings = ProxySettings(
        demo_mode=True,
        api_keys="tenant-a:key-a:free,tenant-b:key-b:pro",
    )
    app = create_app(settings)
    client = TestClient(app)
    app.state.plinth.gate.record_tokens("tenant-b", 200_000)
    body = json.loads((FIXTURES / "demo_request.json").read_text(encoding="utf-8"))
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer key-b"},
        json=body,
    )
    assert r.status_code == 200


def test_open_mode_uses_demo_tier(app_with_free_keyed):
    # No api_keys → demo tier (default enterprise) → no gating.
    app = create_app(ProxySettings(demo_mode=True))
    client = TestClient(app)
    body = json.loads((FIXTURES / "demo_request.json").read_text(encoding="utf-8"))
    r = client.post("/v1/chat/completions", json=body)
    assert r.status_code == 200


def test_chat_records_shaped_tokens_against_budget(app_with_free_keyed):
    client = TestClient(app_with_free_keyed)
    body = json.loads((FIXTURES / "demo_request.json").read_text(encoding="utf-8"))
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer key-a"},
        json=body,
    )
    assert r.status_code == 200
    used = app_with_free_keyed.state.plinth.gate.usage("tenant-a")
    # Shaped order JSON is ~80 tokens, plus any subsequent shaping.
    assert 50 <= used <= 500
