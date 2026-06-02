# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Tests for the control-plane API (``/api/app/*``) backing the /app dashboard.

The downstreams (proxy/gateway/identity) are NOT mocked here — the control
store is the system of record and the live overlay is best-effort, so every
endpoint must work with the downstreams unreachable.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from plinth_dashboard.app_api import _attribution_summary
from plinth_dashboard.server import create_app
from plinth_dashboard.settings import Settings


@asynccontextmanager
async def _auth_client():
    """A dashboard client with app_auth_required=True (JWT enforced)."""
    s = Settings(
        app_auth_required=True,
        identity_url="http://identity.test",
        proxy_url="http://proxy.test",
        gateway_url="http://gateway.test",
        workspace_url="http://workspace.test",
        mock_mcp_url="http://mock.test",
        api_token="Bearer t",
        log_level="WARNING",
    )
    app = create_app(s)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://testserver") as c,
        app.router.lifespan_context(app),
    ):
        yield c


@pytest.mark.asyncio
async def test_state_returns_full_contract(client):
    r = await client.get("/api/app/state")
    assert r.status_code == 200
    state = r.json()
    for key in (
        "kpis",
        "providers",
        "modelAliases",
        "frontDoors",
        "agents",
        "workflows",
        "summaries",
        "toolPolicies",
        "capabilityTokens",
        "guardrails",
        "team",
        "tenants",
        "gatewayTools",
        "auditLog",
        "billing",
        "onboarding",
    ):
        assert key in state, f"missing {key}"
    assert state["kpis"]["reductionPct"] == 71.8
    assert any(p["isDefault"] for p in state["providers"])
    # Rich enough to drive every live list (matches the SSG demo).
    assert len(state["agents"]) >= 12
    assert len(state["workflows"]) >= 6
    assert state["billing"]["invoices"]


@pytest.mark.asyncio
async def test_state_derives_kpis_from_live_telemetry(client):
    """When the proxy savings sink + gateway audit are reachable, the Overview
    KPIs are derived from them — not the seed."""
    with respx.mock(assert_all_called=False, assert_all_mocked=False) as router:
        router.get("http://proxy.test/v1/savings/summary").mock(
            return_value=httpx.Response(
                200,
                json={
                    "total_calls": 5000,
                    "total_raw_tokens": 10_000_000,
                    "total_shaped_tokens": 3_000_000,
                    "total_saved_tokens": 7_000_000,
                    "savings_pct": 0.70,
                    "total_cost_saved_usd": 88.5,
                    "cache_hit_rate": 0.55,
                    "top_connectors_by_savings": [],
                },
            )
        )
        router.get("http://proxy.test/v1/providers").mock(
            return_value=httpx.Response(
                200, json={"providers": ["openai", "groq"], "aliases": [], "default": True}
            )
        )
        router.get("http://gateway.test/v1/audit/stats").mock(
            return_value=httpx.Response(
                200,
                json={
                    "stats": {
                        "total_invocations": 9000,
                        "cached_count": 3600,
                        "error_count": 2,
                        "total_cost_usd": 41.2,
                        "by_tool": [],
                    }
                },
            )
        )
        r = await client.get("/api/app/state")
    assert r.status_code == 200
    state = r.json()
    k = state["kpis"]
    # token reduction + savings from the proxy savings sink
    assert k["reductionPct"] == 70.0
    assert k["rawTokensToday"] == 10_000_000
    assert k["savedTokensToday"] == 7_000_000
    assert k["savedTodayEur"] == 88.5
    # call volume / cache / spend from the gateway audit
    assert k["toolCallsToday"] == 9000
    assert k["cacheHitRate"] == 40.0
    assert k["costTodayEur"] == 41.2
    assert k["costWithoutTodayEur"] == 129.7  # spend + savings
    assert state.get("_telemetry") == "live"


@pytest.mark.asyncio
async def test_state_falls_back_to_seed_when_downstreams_down(client):
    # No respx → downstreams unreachable → seeded KPIs remain (page never breaks).
    r = await client.get("/api/app/state")
    assert r.status_code == 200
    assert r.json()["kpis"]["reductionPct"] == 71.8
    assert r.json().get("_telemetry") != "live"


@pytest.mark.asyncio
async def test_state_overlays_real_tier_into_billing(client):
    """When the proxy /v1/tier is reachable, billing reflects the real plan,
    usage and unlocked features — overriding the seed (tier=pro)."""
    with respx.mock(assert_all_called=False, assert_all_mocked=False) as router:
        router.get("http://proxy.test/v1/tier").mock(
            return_value=httpx.Response(
                200,
                json={
                    "tenant_id": "acme",
                    "tier": "free",
                    "tokens_used_this_month": 80_000,
                    "monthly_token_budget": 100_000,
                    "features": {
                        "pii_redaction": False,
                        "audit_log": False,
                        "custom_rest_connectors": False,
                        "self_hosted_helm": False,
                    },
                },
            )
        )
        state = (await client.get("/api/app/state")).json()
    b = state["billing"]
    assert b["tier"] == "free"
    assert b["usedTokens"] == 80_000
    assert b["monthlyTokenBudget"] == 100_000
    assert b["features"]["pii_redaction"] is False
    assert state.get("_telemetry") == "live"


@pytest.mark.asyncio
async def test_add_provider_persists_in_state(client):
    r = await client.post(
        "/api/app/providers",
        json={
            "name": "Mistral",
            "baseUrl": "https://api.mistral.ai",
            "apiKey": "sk-mistral-secret",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider"]["id"] == "mistral"
    assert body["provider"]["keyMasked"] != "sk-mistral-secret"  # masked, never echoed

    state = (await client.get("/api/app/state")).json()
    assert any(p["id"] == "mistral" for p in state["providers"])


@pytest.mark.asyncio
async def test_add_provider_requires_name_and_url(client):
    r = await client.post("/api/app/providers", json={"name": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_set_default_provider(client):
    await client.post("/api/app/providers", json={"name": "Groq2", "baseUrl": "https://g2.test"})
    r = await client.post("/api/app/providers/groq2/default")
    assert r.status_code == 200
    state = (await client.get("/api/app/state")).json()
    defaults = [p["id"] for p in state["providers"] if p["isDefault"]]
    assert defaults == ["groq2"]  # exactly one default, and it's the new one


@pytest.mark.asyncio
async def test_delete_provider(client):
    await client.post("/api/app/providers", json={"name": "Temp", "baseUrl": "https://t.test"})
    r = await client.delete("/api/app/providers/temp")
    assert r.status_code == 200
    state = (await client.get("/api/app/state")).json()
    assert not any(p["id"] == "temp" for p in state["providers"])


@pytest.mark.asyncio
async def test_alias_add_and_remove(client):
    r = await client.post(
        "/api/app/aliases", json={"alias": "turbo", "target": "groq/llama-3.3-70b"}
    )
    assert r.status_code == 200
    state = (await client.get("/api/app/state")).json()
    assert any(a["alias"] == "turbo" for a in state["modelAliases"])
    r = await client.delete("/api/app/aliases/turbo")
    assert r.status_code == 200
    state = (await client.get("/api/app/state")).json()
    assert not any(a["alias"] == "turbo" for a in state["modelAliases"])


@pytest.mark.asyncio
async def test_toggle_front_door(client):
    r = await client.post("/api/app/frontdoors/bedrock/toggle", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["frontDoor"]["enabled"] is True
    state = (await client.get("/api/app/state")).json()
    assert next(f for f in state["frontDoors"] if f["id"] == "bedrock")["enabled"] is True


@pytest.mark.asyncio
async def test_create_and_pause_agent(client):
    r = await client.post(
        "/api/app/agents", json={"name": "Refund Triage", "model": "fast", "dailyCapEur": 30}
    )
    assert r.status_code == 200
    aid = r.json()["agent"]["id"]
    assert r.json()["agent"]["status"] == "healthy"

    r = await client.post(f"/api/app/agents/{aid}/pause")
    assert r.status_code == 200
    assert r.json()["agent"]["status"] == "paused"

    r = await client.post(f"/api/app/agents/{aid}/bogus")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_token_lifecycle(client):
    # Identity unreachable in tests → falls back to a locally-minted id.
    r = await client.post(
        "/api/app/tokens",
        json={"agent": "agt_x", "scopes": "tool:web.* workspace:ws:rw", "alg": "RS256"},
    )
    assert r.status_code == 200
    tid = r.json()["token"]["id"]
    assert r.json()["token"]["scopes"] == ["tool:web.*", "workspace:ws:rw"]

    r = await client.post(f"/api/app/tokens/{tid}/rotate")
    assert r.status_code == 200
    assert r.json()["token"]["rotatedAgo"] == "just now"

    r = await client.delete(f"/api/app/tokens/{tid}")
    assert r.status_code == 200
    state = (await client.get("/api/app/state")).json()
    assert next(t for t in state["capabilityTokens"] if t["id"] == tid)["status"] == "expired"


@pytest.mark.asyncio
async def test_set_policy(client):
    r = await client.put(
        "/api/app/policies/web.fetch",
        json={"keepFields": "title, text", "maxTokens": 800, "blockWrites": True},
    )
    assert r.status_code == 200
    state = (await client.get("/api/app/state")).json()
    pol = next(p for p in state["toolPolicies"] if p["tool"] == "web.fetch")
    assert pol["keepFields"] == ["title", "text"]
    assert pol["maxTokens"] == 800
    assert pol["blockWrites"] is True


@pytest.mark.asyncio
async def test_guardrail_update(client):
    r = await client.patch(
        "/api/app/guardrails/agt_support", json={"rpmLimit": 200, "dailyCapEur": 75}
    )
    assert r.status_code == 200
    assert r.json()["guardrail"]["rpmLimit"] == 200


@pytest.mark.asyncio
async def test_onboarding_billing_team(client):
    r = await client.post("/api/app/onboarding/point", json={"done": True})
    assert r.status_code == 200
    assert r.json()["step"]["done"] is True

    r = await client.post("/api/app/billing/plan", json={"plan": "Enterprise"})
    assert r.status_code == 200
    assert r.json()["billing"]["plan"] == "Enterprise"

    r = await client.post(
        "/api/app/team/invite", json={"email": "newdev@acme.io", "role": "Developer"}
    )
    assert r.status_code == 200
    state = (await client.get("/api/app/state")).json()
    assert any(m["email"] == "newdev@acme.io" for m in state["team"])

    r = await client.post("/api/app/team/invite", json={"email": "not-an-email"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Real JWT auth (app_auth_required=True): issue on /session, verify on /app/*


@pytest.mark.asyncio
async def test_app_auth_rejects_missing_token():
    async with _auth_client() as c:
        r = await c.get("/api/app/state")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.asyncio
async def test_app_auth_accepts_verified_token():
    async with _auth_client() as c:
        with respx.mock(assert_all_called=False, assert_all_mocked=False) as router:
            router.post("http://identity.test/v1/tokens/verify").mock(
                return_value=httpx.Response(200, json={"sub": "u", "tenant_id": "acme"})
            )
            r = await c.get("/api/app/state", headers={"Authorization": "Bearer good-jwt"})
        assert r.status_code == 200
        assert "kpis" in r.json()


@pytest.mark.asyncio
async def test_app_auth_rejects_token_identity_denies():
    async with _auth_client() as c:
        with respx.mock(assert_all_called=False, assert_all_mocked=False) as router:
            router.post("http://identity.test/v1/tokens/verify").mock(
                return_value=httpx.Response(401, json={"error": {"code": "INVALID_TOKEN"}})
            )
            r = await c.get("/api/app/state", headers={"Authorization": "Bearer bad-jwt"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_session_issues_jwt_from_identity():
    # /session is exempt from auth (it mints the token) and proxies to identity.
    async with _auth_client() as c:
        with respx.mock(assert_all_called=False, assert_all_mocked=False) as router:
            router.post("http://identity.test/v1/tokens").mock(
                return_value=httpx.Response(
                    201,
                    json={"token": "jwt-abc", "jti": "jti_1", "claims": {"sub": "u@acme.io"}},
                )
            )
            r = await c.post("/api/app/session", json={"email": "u@acme.io"})
        assert r.status_code == 200
        body = r.json()
        assert body["token"] == "jwt-abc"
        assert body["claims"]["sub"] == "u@acme.io"


# ---------------------------------------------------------------------------
# Partner attribution (?ref= → recorded + baked into token metadata)


@pytest.mark.asyncio
async def test_session_records_partner_referral(client):
    # Identity unreachable here → demo fallback, but the ref is still recorded.
    r = await client.post("/api/app/session", json={"email": "u@acme.io", "ref": "cursor"})
    assert r.status_code == 200
    assert r.json()["ref"] == "cursor"
    state = (await client.get("/api/app/state")).json()
    assert state.get("referrals", {}).get("cursor") == 1


@pytest.mark.asyncio
async def test_session_passes_ref_into_token_metadata():
    async with _auth_client() as c:
        with respx.mock(assert_all_called=False, assert_all_mocked=False) as router:
            route = router.post("http://identity.test/v1/tokens").mock(
                return_value=httpx.Response(201, json={"token": "jwt", "claims": {}})
            )
            r = await c.post("/api/app/session", json={"email": "u@acme.io", "ref": "zapier"})
        assert r.status_code == 200
        sent = json.loads(route.calls.last.request.content)
        assert sent["metadata"]["ref"] == "zapier"


def test_attribution_summary_rolls_up_and_sorts():
    summary = _attribution_summary({"a": 2, "b": 9, "c": 0, "d": 9})
    # Zero-count sources are dropped; ties break alphabetically by ref.
    assert summary["totalReferred"] == 20
    assert summary["uniqueSources"] == 3
    assert [s["ref"] for s in summary["sources"]] == ["b", "d", "a"]


def test_attribution_summary_handles_empty():
    summary = _attribution_summary({})
    assert summary == {"totalReferred": 0, "uniqueSources": 0, "sources": []}


@pytest.mark.asyncio
async def test_state_exposes_attribution_from_seed(client):
    state = (await client.get("/api/app/state")).json()
    attr = state["attribution"]
    # Seeded demo referrals: 18+12+7+5+3 = 45 across 5 sources, biggest first.
    assert attr["totalReferred"] == 45
    assert attr["uniqueSources"] == 5
    assert attr["sources"][0] == {"ref": "n8n-listing", "count": 18}


@pytest.mark.asyncio
async def test_recording_a_referral_bumps_attribution(client):
    await client.post("/api/app/session", json={"email": "u@acme.io", "ref": "cursor"})
    attr = (await client.get("/api/app/state")).json()["attribution"]
    assert attr["totalReferred"] == 46
    assert attr["uniqueSources"] == 6
    assert {"ref": "cursor", "count": 1} in attr["sources"]
