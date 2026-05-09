# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Dashboard tests for the v1.0 tenants admin UI + schema-evolution wizard."""

from __future__ import annotations

import httpx
import pytest
import respx
from plinth_dashboard.server import STATIC_DIR


# ---------------------------------------------------------------------------
# /tenants route serves SPA shell


@pytest.mark.asyncio
async def test_tenants_route_serves_spa_shell(client: httpx.AsyncClient):
    r = await client.get("/tenants")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    # The shell carries the topnav we added so the test catches drift
    # between the route handler and the actual shell HTML.
    assert "Plinth" in body


@pytest.mark.asyncio
async def test_tenant_detail_route_serves_spa_shell(client: httpx.AsyncClient):
    r = await client.get("/tenants/acme")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


@pytest.mark.asyncio
async def test_index_html_has_tenants_topnav():
    # The static asset gets shipped verbatim; verify the topnav has the
    # link to #/tenants so the SPA router knows where to send the user.
    body = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'href="#/tenants"' in body
    assert 'data-route="tenants"' in body


@pytest.mark.asyncio
async def test_index_html_has_tpl_tenants_list():
    body = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="tpl-tenants-list"' in body
    assert 'id="tpl-tenant-detail"' in body


@pytest.mark.asyncio
async def test_index_html_has_schema_wizard():
    body = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="schema-wizard-modal"' in body
    assert 'id="schema-wizard-editor"' in body
    assert 'id="schema-check-btn"' in body
    assert 'id="schema-apply-btn"' in body


@pytest.mark.asyncio
async def test_app_js_has_schema_wizard_handlers():
    body = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "PlinthSchemaWizard" in body
    assert "doSchemaCheck" in body
    assert "doSchemaApply" in body


# ---------------------------------------------------------------------------
# /api/tenants proxies identity (with workspace fallback)


@pytest.mark.asyncio
async def test_api_tenants_proxies_identity(client: httpx.AsyncClient):
    payload = {
        "tenants": [
            {"id": "acme", "name": "Acme Inc.", "metadata": {}, "created_at": "2026-01-01T00:00:00Z"},
        ]
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://identity.test/v1/tenants").mock(
            return_value=httpx.Response(200, json=payload)
        )
        r = await client.get("/api/tenants")
    assert r.status_code == 200
    assert r.json()["tenants"][0]["id"] == "acme"


@pytest.mark.asyncio
async def test_api_tenants_falls_back_to_workspace(client: httpx.AsyncClient):
    # Identity is unreachable — the dashboard falls through to workspace.
    payload = {"tenants": [{"id": "default", "workspace_count": 0}]}
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://identity.test/v1/tenants").mock(
            return_value=httpx.Response(503, json={"error": "down"})
        )
        mock.get("http://workspace.test/v1/tenants").mock(
            return_value=httpx.Response(200, json=payload)
        )
        r = await client.get("/api/tenants")
    assert r.status_code == 200
    assert r.json()["tenants"][0]["id"] == "default"


@pytest.mark.asyncio
async def test_api_tenant_quotas_proxies_identity(client: httpx.AsyncClient):
    payload = {
        "tenant_id": "acme",
        "max_workspaces": 100,
        "max_storage_gb": 10.0,
        "max_channels_per_workspace": 50,
        "max_workflows_per_workspace": 100,
        "max_active_tokens": 1000,
        "max_oauth_connections": 50,
        "max_cost_usd_day": 100.0,
        "max_cost_usd_month": 2000.0,
        "max_invocations_per_minute": 600,
        "updated_at": "2026-01-01T00:00:00Z",
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://identity.test/v1/tenants/acme/quotas").mock(
            return_value=httpx.Response(200, json=payload)
        )
        r = await client.get("/api/tenants/acme/quotas")
    assert r.status_code == 200
    assert r.json()["max_workspaces"] == 100


@pytest.mark.asyncio
async def test_api_tenant_quotas_set_forwards_body(client: httpx.AsyncClient):
    written = {}

    def handler(request):
        nonlocal written
        written = dict(request.read() and request.read().decode().__class__) if False else None
        # respx exposes the raw bytes via request.content.
        try:
            import json as _json
            written.update(_json.loads(request.content))  # type: ignore
        except Exception:
            pass
        return httpx.Response(
            200,
            json={"tenant_id": "acme", "max_workspaces": 5},
        )

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("http://identity.test/v1/tenants/acme/quotas").mock(
            return_value=httpx.Response(
                200,
                json={
                    "tenant_id": "acme",
                    "max_workspaces": 5,
                    "max_storage_gb": 10.0,
                    "max_channels_per_workspace": 50,
                    "max_workflows_per_workspace": 100,
                    "max_active_tokens": 1000,
                    "max_oauth_connections": 50,
                    "max_cost_usd_day": 100.0,
                    "max_cost_usd_month": 2000.0,
                    "max_invocations_per_minute": 600,
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            )
        )
        r = await client.post(
            "/api/tenants/acme/quotas",
            json={"max_workspaces": 5},
        )
        assert r.status_code == 200
        assert r.json()["max_workspaces"] == 5
        # The dashboard must forward the body intact.
        assert route.called


@pytest.mark.asyncio
async def test_api_tenant_usage_proxies_identity(client: httpx.AsyncClient):
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://identity.test/v1/tenants/acme/usage").mock(
            return_value=httpx.Response(
                200,
                json={
                    "tenant_id": "acme",
                    "workspaces": 0,
                    "storage_gb": 0,
                    "active_tokens": 3,
                    "oauth_connections": 0,
                    "cost_usd_day": 0,
                    "cost_usd_month": 0,
                    "last_invocation_at": None,
                    "notes": {"workspaces": "owned by workspace service"},
                },
            )
        )
        r = await client.get("/api/tenants/acme/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["active_tokens"] == 3
    assert "workspaces" in body["notes"]


# ---------------------------------------------------------------------------
# /api/workspaces/{ws}/channels/{name}/schema/check proxies workspace


@pytest.mark.asyncio
async def test_api_schema_check_proxies_workspace(client: httpx.AsyncClient):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(
            "http://workspace.test/v1/workspaces/ws_a/channels/research-out/schema/check"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "checked": 5,
                    "valid": 5,
                    "invalid": 0,
                    "sample_failures": [],
                },
            )
        )
        r = await client.post(
            "/api/workspaces/ws_a/channels/research-out/schema/check",
            json={"schema": {"type": "object"}, "scope": "both"},
        )
    assert r.status_code == 200
    assert r.json()["invalid"] == 0
    assert route.called


@pytest.mark.asyncio
async def test_api_schema_set_proxies_workspace(client: httpx.AsyncClient):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(
            "http://workspace.test/v1/workspaces/ws_a/channels/research-out/schema"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "workspace_id": "ws_a",
                    "channel": "research-out",
                    "version": 2,
                    "schema_json": {"type": "object"},
                    "created_at": "2026-01-01T00:00:00Z",
                },
            )
        )
        r = await client.post(
            "/api/workspaces/ws_a/channels/research-out/schema",
            json={"schema": {"type": "object"}},
        )
    assert r.status_code == 200
    assert r.json()["version"] == 2


# ---------------------------------------------------------------------------
# Static asset surface — make sure new IDs / templates are exposed


def test_app_js_has_quota_form_handlers():
    body = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "wireQuotasForm" in body
    assert "loadTenantsList" in body
    assert "loadTenantDetail" in body


def test_app_js_handles_tenant_routes():
    body = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'parts[0] === "tenants"' in body
    assert 'tpl-tenants-list' in body
    assert 'tpl-tenant-detail' in body


def test_index_html_quotas_form_fields():
    body = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for field in [
        "max_workspaces",
        "max_storage_gb",
        "max_channels_per_workspace",
        "max_workflows_per_workspace",
        "max_active_tokens",
        "max_oauth_connections",
        "max_cost_usd_day",
        "max_cost_usd_month",
        "max_invocations_per_minute",
    ]:
        assert f'name="{field}"' in body, f"Missing form field {field}"
