# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""End-to-end tests for the dashboard FastAPI app (server.py).

Each test boots an in-process app via ``ASGITransport`` and uses ``respx``
to mock the upstream services. The dashboard is read-only, so every
endpoint we cover is a ``GET``.
"""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

from plinth_dashboard import __version__
from plinth_dashboard.settings import Settings

# ---------------------------------------------------------------------------
# Liveness + UI


async def test_healthz_ok(client: AsyncClient):
    r = await client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "version": __version__, "service": "dashboard"}


async def test_index_serves_html(client: AsyncClient):
    r = await client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Plinth" in r.text


async def test_workspace_subroute_serves_spa_shell(client: AsyncClient):
    r = await client.get("/workspaces/ws_abc")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Plinth" in r.text


async def test_static_app_js(client: AsyncClient):
    r = await client.get("/static/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


async def test_static_style_css(client: AsyncClient):
    r = await client.get("/static/style.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


async def test_favicon_route(client: AsyncClient):
    r = await client.get("/favicon.ico")
    assert r.status_code == 200
    assert "image/svg" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# /api/overview shape


async def test_api_overview_shape(
    client: AsyncClient, settings: Settings, workspace_factory, audit_stats_factory
):
    """Overview endpoint returns the documented JSON shape."""
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/healthz").mock(
            return_value=httpx.Response(
                200, json={"status": "ok", "version": "0.1.0", "service": "workspace"}
            )
        )
        router.get(f"{settings.workspace_url}/v1/workspaces").mock(
            return_value=httpx.Response(
                200,
                json={"workspaces": [workspace_factory(ws_id="ws_a", name="alpha")]},
            )
        )
        router.get(f"{settings.gateway_url}/healthz").mock(
            return_value=httpx.Response(
                200, json={"status": "ok", "version": "0.1.0", "service": "gateway"}
            )
        )
        router.get(f"{settings.gateway_url}/v1/audit/stats").mock(
            return_value=httpx.Response(200, json=audit_stats_factory())
        )
        router.get(f"{settings.gateway_url}/v1/cache/stats").mock(
            return_value=httpx.Response(
                200,
                json={"hits": 38, "misses": 104, "entries": 67, "size_bytes": 412341},
            )
        )
        router.get(f"{settings.gateway_url}/v1/tools").mock(
            return_value=httpx.Response(200, json={"tools": [{"tool_id": "x"}]})
        )
        router.get(f"{settings.mock_mcp_url}/healthz").mock(
            return_value=httpx.Response(
                200, json={"status": "ok", "version": "0.1.0", "service": "mock-mcp"}
            )
        )
        # v0.3 — identity service + tenants endpoints
        router.get(f"{settings.identity_url}/healthz").mock(
            return_value=httpx.Response(
                200, json={"status": "ok", "version": "0.3.0", "service": "identity"}
            )
        )
        router.get(f"{settings.workspace_url}/v1/tenants").mock(
            return_value=httpx.Response(
                200, json={"tenants": [{"id": "default", "workspace_count": 1}]}
            )
        )
        router.get(f"{settings.gateway_url}/v1/tenants").mock(
            return_value=httpx.Response(
                200,
                json={
                    "tenants": [{"id": "default", "audit_count": 42, "tool_count": 1}]
                },
            )
        )
        # v0.4 — observability status + recent events for the time-series.
        router.get(f"{settings.gateway_url}/v1/observability/status").mock(
            return_value=httpx.Response(
                200,
                json={
                    "otlp_enabled": False,
                    "otlp_endpoint": None,
                    "events_emitted": 0,
                    "last_emit_at": None,
                    "flush_errors": 0,
                },
            )
        )
        router.get(f"{settings.gateway_url}/v1/audit").mock(
            return_value=httpx.Response(200, json={"events": []})
        )
        r = await client.get("/api/overview")

    assert r.status_code == 200
    body = r.json()
    # Top-level keys per spec
    for key in (
        "services",
        "workspaces",
        "audit",
        "cache",
        "tools",
        "tenants",
        "fetched_at",
    ):
        assert key in body
    assert body["workspaces"]["count"] == 1
    assert body["workspaces"]["list"][0]["id"] == "ws_a"
    assert body["audit"]["total_invocations"] == 142
    assert body["cache"]["hits"] == 38
    assert body["tools"]["count"] == 1
    assert body["tenants"]["count"] == 1
    assert body["tenants"]["list"][0]["id"] == "default"
    assert body["services"]["identity"]["status"] == "up"


# ---------------------------------------------------------------------------
# Proxies


async def test_proxy_workspaces(client: AsyncClient, settings: Settings):
    payload = {"workspaces": [{"id": "ws_a", "name": "research-1"}]}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/v1/workspaces").mock(
            return_value=httpx.Response(200, json=payload)
        )
        r = await client.get("/api/workspaces")
    assert r.status_code == 200
    assert r.json() == payload


async def test_proxy_workspace_detail(client: AsyncClient, settings: Settings):
    payload = {"id": "ws_a", "name": "research-1"}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/v1/workspaces/ws_a").mock(
            return_value=httpx.Response(200, json=payload)
        )
        r = await client.get("/api/workspaces/ws_a")
    assert r.status_code == 200
    assert r.json() == payload


async def test_proxy_workspace_kv(client: AsyncClient, settings: Settings):
    payload = {"entries": [{"key": "topic", "value": "x", "version": 1}]}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/v1/workspaces/ws_a/kv").mock(
            return_value=httpx.Response(200, json=payload)
        )
        r = await client.get("/api/workspaces/ws_a/kv")
    assert r.status_code == 200
    assert r.json() == payload


async def test_proxy_workspace_snapshots(client: AsyncClient, settings: Settings):
    payload = {"snapshots": [{"id": "snap_1", "name": "baseline"}]}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/v1/workspaces/ws_a/snapshots").mock(
            return_value=httpx.Response(200, json=payload)
        )
        r = await client.get("/api/workspaces/ws_a/snapshots")
    assert r.status_code == 200
    assert r.json() == payload


async def test_proxy_workspace_channels(client: AsyncClient, settings: Settings):
    payload = {"channels": [{"name": "out", "message_count": 3}]}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/v1/workspaces/ws_a/channels").mock(
            return_value=httpx.Response(200, json=payload)
        )
        r = await client.get("/api/workspaces/ws_a/channels")
    assert r.status_code == 200
    assert r.json() == payload


async def test_proxy_workspace_workflows(client: AsyncClient, settings: Settings):
    payload = {"workflows": [{"id": "wf_1", "name": "research"}]}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/v1/workspaces/ws_a/workflows").mock(
            return_value=httpx.Response(200, json=payload)
        )
        r = await client.get("/api/workspaces/ws_a/workflows")
    assert r.status_code == 200
    assert r.json() == payload


async def test_proxy_audit_forwards_query(client: AsyncClient, settings: Settings):
    """Audit proxy forwards query params unchanged to the gateway."""
    payload = {"events": []}
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{settings.gateway_url}/v1/audit").mock(
            return_value=httpx.Response(200, json=payload)
        )
        r = await client.get("/api/audit?limit=10&workspace_id=ws_a")
    assert r.status_code == 200
    # respx records the call URL with query params
    assert route.called
    called_url = str(route.calls.last.request.url)
    assert "limit=10" in called_url
    assert "workspace_id=ws_a" in called_url


async def test_proxy_cache_stats(client: AsyncClient, settings: Settings):
    payload = {"hits": 1, "misses": 2, "entries": 1, "size_bytes": 100}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.gateway_url}/v1/cache/stats").mock(
            return_value=httpx.Response(200, json=payload)
        )
        r = await client.get("/api/cache-stats")
    assert r.status_code == 200
    assert r.json() == payload


async def test_proxy_audit_stats(client: AsyncClient, settings: Settings):
    payload = {"stats": {"total_invocations": 0}}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.gateway_url}/v1/audit/stats").mock(
            return_value=httpx.Response(200, json=payload)
        )
        r = await client.get("/api/audit-stats")
    assert r.status_code == 200
    assert r.json() == payload


async def test_proxy_tools(client: AsyncClient, settings: Settings):
    payload = {"tools": [{"tool_id": "web.fetch"}, {"tool_id": "web.search"}]}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.gateway_url}/v1/tools").mock(
            return_value=httpx.Response(200, json=payload)
        )
        r = await client.get("/api/tools")
    assert r.status_code == 200
    assert r.json() == payload


# ---------------------------------------------------------------------------
# Failure modes


async def test_proxy_502_when_upstream_unreachable(
    client: AsyncClient, settings: Settings
):
    """If httpx raises (connection refused, DNS error), the dashboard returns 502."""
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/v1/workspaces").mock(
            side_effect=httpx.ConnectError("workspace down")
        )
        r = await client.get("/api/workspaces")
    assert r.status_code == 502
    body = r.json()
    assert body["error"]["code"] == "UPSTREAM_UNREACHABLE"
    assert "workspace down" in body["error"]["details"]["reason"]


async def test_proxy_passes_through_upstream_status(
    client: AsyncClient, settings: Settings
):
    """A 4xx/5xx upstream is returned with the original status + body."""
    err_body = {"error": {"code": "WORKSPACE_NOT_FOUND", "message": "no such ws"}}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/v1/workspaces/missing").mock(
            return_value=httpx.Response(404, json=err_body)
        )
        r = await client.get("/api/workspaces/missing")
    assert r.status_code == 404
    assert r.json() == err_body


async def test_proxy_non_json_upstream(client: AsyncClient, settings: Settings):
    """Non-JSON upstream → wrapped error envelope; preserves status code."""
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.gateway_url}/v1/tools").mock(
            return_value=httpx.Response(
                200, content=b"<html>not json</html>", headers={"Content-Type": "text/html"}
            )
        )
        r = await client.get("/api/tools")
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["code"] == "UPSTREAM_NON_JSON"
