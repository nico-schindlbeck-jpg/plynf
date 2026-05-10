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


# ---------------------------------------------------------------------------
# v0.6 — workflow visualization
# ---------------------------------------------------------------------------


def _wf(
    *,
    wf_id: str = "wf_a",
    workspace_id: str = "ws_a",
    name: str = "research-pipeline",
    status: str = "running",
    manifest: list[str] | None = None,
    steps: list[dict] | None = None,
    created_at: str = "2026-05-07T16:00:00Z",
    started_at: str | None = "2026-05-07T16:01:00Z",
    finished_at: str | None = None,
) -> dict:
    """Build a workspace-shaped workflow document for fixtures."""
    return {
        "id": wf_id,
        "workspace_id": workspace_id,
        "name": name,
        "steps_manifest": manifest if manifest is not None else ["search", "fetch"],
        "steps": steps or [],
        "status": status,
        "metadata": {},
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
    }


def _step(
    *,
    step_id: str = "step_1",
    workflow_id: str = "wf_a",
    name: str = "search",
    status: str = "completed",
    attempt: int = 1,
    started_at: str | None = "2026-05-07T16:01:00Z",
    finished_at: str | None = "2026-05-07T16:01:05Z",
) -> dict:
    return {
        "id": step_id,
        "workflow_id": workflow_id,
        "name": name,
        "status": status,
        "attempt": attempt,
        "started_at": started_at,
        "finished_at": finished_at,
        "input": None,
        "output": None,
        "error": None,
        "snapshot_id": None,
        "created_at": "2026-05-07T16:00:00Z",
    }


async def test_api_workflows_overview_aggregates_across_workspaces(
    client: AsyncClient, settings: Settings, workspace_factory
):
    """Aggregator collects all workflows from every workspace into one list."""
    ws_a = workspace_factory(ws_id="ws_a", name="research-1")
    ws_b = workspace_factory(ws_id="ws_b", name="pipeline-2")

    wf_a1 = _wf(
        wf_id="wf_a1",
        workspace_id="ws_a",
        name="research-pipeline",
        status="running",
        manifest=["search", "fetch", "extract"],
        steps=[
            _step(step_id="s1", workflow_id="wf_a1", name="search", status="completed"),
            _step(
                step_id="s2",
                workflow_id="wf_a1",
                name="fetch",
                status="running",
                finished_at=None,
            ),
        ],
        created_at="2026-05-07T16:00:00Z",
    )
    wf_b1 = _wf(
        wf_id="wf_b1",
        workspace_id="ws_b",
        name="etl",
        status="completed",
        manifest=["load", "save"],
        steps=[
            _step(step_id="s3", workflow_id="wf_b1", name="load", status="completed"),
            _step(step_id="s4", workflow_id="wf_b1", name="save", status="completed"),
        ],
        created_at="2026-05-07T15:00:00Z",
        finished_at="2026-05-07T15:10:00Z",
    )

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/v1/workspaces").mock(
            return_value=httpx.Response(200, json={"workspaces": [ws_a, ws_b]})
        )
        router.get(f"{settings.workspace_url}/v1/workspaces/ws_a/workflows").mock(
            return_value=httpx.Response(200, json={"workflows": [wf_a1]})
        )
        router.get(f"{settings.workspace_url}/v1/workspaces/ws_b/workflows").mock(
            return_value=httpx.Response(200, json={"workflows": [wf_b1]})
        )
        r = await client.get("/api/workflows/overview")

    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["partial"] is False
    assert {w["workflow_id"] for w in body["workflows"]} == {"wf_a1", "wf_b1"}
    # Newest-first sort: wf_a1 (2026-05-07T16:00) before wf_b1 (15:00).
    assert body["workflows"][0]["workflow_id"] == "wf_a1"
    by_status = body["by_status"]
    assert by_status["running"] == 1
    assert by_status["completed"] == 1
    # Step counts derive from manifest length.
    a = next(w for w in body["workflows"] if w["workflow_id"] == "wf_a1")
    assert a["step_count"] == 3
    assert a["completed_count"] == 1
    assert a["running_count"] == 1
    assert a["pending_count"] == 1  # extract has no recorded attempt yet
    assert a["workspace_name"] == "research-1"


async def test_api_workflows_overview_partial_when_workspace_listing_fails(
    client: AsyncClient, settings: Settings, workspace_factory
):
    """A failed per-workspace /workflows call sets partial=true; other workspaces still flow."""
    ws_a = workspace_factory(ws_id="ws_a", name="alpha")
    ws_b = workspace_factory(ws_id="ws_b", name="beta")

    wf = _wf(wf_id="wf_x", workspace_id="ws_a", name="ok", status="completed")

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/v1/workspaces").mock(
            return_value=httpx.Response(200, json={"workspaces": [ws_a, ws_b]})
        )
        router.get(f"{settings.workspace_url}/v1/workspaces/ws_a/workflows").mock(
            return_value=httpx.Response(200, json={"workflows": [wf]})
        )
        # ws_b is unreachable.
        router.get(f"{settings.workspace_url}/v1/workspaces/ws_b/workflows").mock(
            side_effect=httpx.ConnectError("ws_b unreachable")
        )
        r = await client.get("/api/workflows/overview")

    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["partial"] is True
    assert body["workflows"][0]["workflow_id"] == "wf_x"


async def test_api_workflows_overview_empty_when_no_workspaces(
    client: AsyncClient, settings: Settings
):
    """Zero workspaces → empty list, total: 0, partial: false."""
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/v1/workspaces").mock(
            return_value=httpx.Response(200, json={"workspaces": []})
        )
        r = await client.get("/api/workflows/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["workflows"] == []
    assert body["partial"] is False
    assert body["by_status"] == {
        "pending": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
    }


async def test_api_workflows_overview_empty_when_workspace_service_down(
    client: AsyncClient, settings: Settings
):
    """If the workspaces listing itself fails, return empty list with partial=true."""
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/v1/workspaces").mock(
            side_effect=httpx.ConnectError("workspace service down")
        )
        r = await client.get("/api/workflows/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["workflows"] == []
    assert body["partial"] is True


async def test_proxy_workspace_workflow_detail(
    client: AsyncClient, settings: Settings
):
    """Per-workflow proxy returns the workspace's full workflow document."""
    payload = _wf(wf_id="wf_a1", workspace_id="ws_a", name="r")
    with respx.mock(assert_all_called=False) as router:
        router.get(
            f"{settings.workspace_url}/v1/workspaces/ws_a/workflows/wf_a1"
        ).mock(return_value=httpx.Response(200, json=payload))
        r = await client.get("/api/workspaces/ws_a/workflows/wf_a1")
    assert r.status_code == 200
    assert r.json() == payload


async def test_workflows_route_serves_spa_shell(client: AsyncClient):
    """/workflows path serves the SPA shell (HTML)."""
    r = await client.get("/workflows")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Plinth" in r.text


async def test_workflow_detail_route_serves_spa_shell(client: AsyncClient):
    """/workflows/<wf_id> path serves the SPA shell (HTML)."""
    r = await client.get("/workflows/wf_abc")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Plinth" in r.text


async def test_index_html_has_workflows_navigation(client: AsyncClient):
    """The SPA shell ships the workflow templates and topbar nav link."""
    r = await client.get("/")
    assert r.status_code == 200
    text = r.text
    assert 'id="tpl-workflows-list"' in text
    assert 'id="tpl-workflow-detail"' in text
    assert 'data-route="workflows"' in text
    assert 'href="#/workflows"' in text


async def test_static_app_js_includes_workflow_render(client: AsyncClient):
    """Smoke-check that the SPA bundles the new workflow code paths."""
    r = await client.get("/static/app.js")
    assert r.status_code == 200
    js = r.text
    assert "renderWorkflowGraph" in js
    assert "refreshWorkflowsList" in js
    assert "openWorkflowStepModal" in js


async def test_static_style_css_includes_workflow_node_classes(
    client: AsyncClient,
):
    """Style sheet ships the new graph-node and list-status classes."""
    r = await client.get("/static/style.css")
    assert r.status_code == 200
    css = r.text
    for cls in (
        ".wf-node--pending",
        ".wf-node--running",
        ".wf-node--completed",
        ".wf-node--failed",
        ".wf-node--cancelled",
        ".wf-list-status-icon--running",
    ):
        assert cls in css


# ---------------------------------------------------------------------------
# v0.6 — DLQ batch op proxies (replay-all, purge)
# ---------------------------------------------------------------------------


async def test_proxy_replay_all_forwards_body(
    client: AsyncClient, settings: Settings
):
    """``POST /api/.../replay-all`` forwards the JSON body untouched."""
    upstream_payload = {
        "channel": "out",
        "attempted": 3,
        "succeeded": 2,
        "failed": 1,
        "failures": [{"msg_id": "msg_x", "reason": "still invalid"}],
        "dry_run": False,
    }
    captured: dict = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["query"] = dict(request.url.params)
        return httpx.Response(200, json=upstream_payload)

    with respx.mock(assert_all_called=True) as router:
        router.post(
            f"{settings.workspace_url}/v1/workspaces/ws_a/channels/out/deadletter/replay-all"
        ).mock(side_effect=_record)
        r = await client.post(
            "/api/workspaces/ws_a/channels/out/deadletter/replay-all",
            json={"dry_run": True, "max": 50},
        )
    assert r.status_code == 200
    assert r.json() == upstream_payload
    # The body the upstream saw is JSON-equivalent to what the SPA sent —
    # we compare parsed JSON rather than raw bytes because httpx may
    # canonicalise spacing.
    import json as _json

    assert _json.loads(captured["body"]) == {"dry_run": True, "max": 50}


async def test_proxy_replay_all_forwards_query_when_no_body(
    client: AsyncClient, settings: Settings
):
    """The query-string form (``?max=10``) is forwarded too."""
    upstream_payload = {
        "channel": "out",
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "failures": [],
        "dry_run": False,
    }
    captured: dict = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["query"] = dict(request.url.params)
        return httpx.Response(200, json=upstream_payload)

    with respx.mock(assert_all_called=True) as router:
        router.post(
            f"{settings.workspace_url}/v1/workspaces/ws_a/channels/out/deadletter/replay-all"
        ).mock(side_effect=_record)
        r = await client.post(
            "/api/workspaces/ws_a/channels/out/deadletter/replay-all?max=10"
        )
    assert r.status_code == 200
    assert captured["query"] == {"max": "10"}


async def test_proxy_purge_dlq_forwards_query(
    client: AsyncClient, settings: Settings
):
    """``DELETE /api/.../deadletter`` forwards the ``older_than_seconds`` query."""
    upstream_payload = {"purged": 7}
    captured: dict = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["query"] = dict(request.url.params)
        captured["method"] = request.method
        return httpx.Response(200, json=upstream_payload)

    with respx.mock(assert_all_called=True) as router:
        router.delete(
            f"{settings.workspace_url}/v1/workspaces/ws_a/channels/out/deadletter"
        ).mock(side_effect=_record)
        r = await client.delete(
            "/api/workspaces/ws_a/channels/out/deadletter?older_than_seconds=86400"
        )
    assert r.status_code == 200
    assert r.json() == upstream_payload
    assert captured["method"] == "DELETE"
    assert captured["query"] == {"older_than_seconds": "86400"}


async def test_proxy_replay_all_propagates_upstream_error(
    client: AsyncClient, settings: Settings
):
    """A 4xx from the workspace makes it back to the SPA verbatim."""
    err_body = {
        "error": {
            "code": "INVALID_ARGUMENTS",
            "message": "something",
            "details": {},
        }
    }
    with respx.mock(assert_all_called=True) as router:
        router.post(
            f"{settings.workspace_url}/v1/workspaces/ws_a/channels/out/deadletter/replay-all"
        ).mock(return_value=httpx.Response(400, json=err_body))
        r = await client.post(
            "/api/workspaces/ws_a/channels/out/deadletter/replay-all",
            json={},
        )
    assert r.status_code == 400
    assert r.json() == err_body


async def test_static_app_js_includes_dlq_batch_buttons(client: AsyncClient):
    """The SPA bundle ships the new ``Replay all`` / ``Purge`` handlers."""
    r = await client.get("/static/app.js")
    assert r.status_code == 200
    js = r.text
    # Function names + button IDs we wire up in app.js.
    for needle in (
        "dlq-replay-all",
        "dlq-purge-old",
        "replayAllDeadletters",
        "purgeOldDeadletters",
    ):
        assert needle in js, f"missing {needle!r} in app.js"


async def test_index_html_has_dlq_batch_buttons(client: AsyncClient):
    """The DLQ modal markup ships the two new action buttons."""
    r = await client.get("/")
    assert r.status_code == 200
    text = r.text
    assert 'id="dlq-replay-all"' in text
    assert 'id="dlq-purge-old"' in text


# ---------------------------------------------------------------------------
# v1.4 — cost-by-agent + anomalies proxies


async def test_proxy_cost_by_agent_aggregates(
    client: AsyncClient, settings: Settings
):
    """Dashboard proxy returns the gateway's cost-by-agent payload verbatim."""
    payload = {
        "window": "24h",
        "window_start": "2026-05-09T12:00:00+00:00",
        "window_end": "2026-05-10T12:00:00+00:00",
        "agents": [
            {
                "agent_id": "ag_a",
                "tenant_id": "default",
                "invocations": 12,
                "cached_invocations": 4,
                "total_cost_usd": 0.42,
                "avg_duration_ms": 132.5,
                "top_tools": [
                    {"tool_id": "web.fetch", "invocations": 7, "cost_usd": 0.30},
                    {"tool_id": "web.search", "invocations": 5, "cost_usd": 0.12},
                ],
            }
        ],
        "total_agents": 1,
        "total_cost_usd": 0.42,
        "fetched_at": "2026-05-10T12:00:00+00:00",
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.get(
            f"{settings.gateway_url}/v1/audit/cost-by-agent"
        ).mock(return_value=httpx.Response(200, json=payload))
        r = await client.get("/api/cost-by-agent?window=24h&top=5")
    assert r.status_code == 200
    body = r.json()
    assert body == payload
    # Ensure query params were forwarded.
    called_url = str(route.calls.last.request.url)
    assert "window=24h" in called_url
    assert "top=5" in called_url


async def test_proxy_cost_by_agent_empty(
    client: AsyncClient, settings: Settings
):
    payload = {
        "window": "24h",
        "window_start": "2026-05-09T12:00:00+00:00",
        "window_end": "2026-05-10T12:00:00+00:00",
        "agents": [],
        "total_agents": 0,
        "total_cost_usd": 0.0,
        "fetched_at": "2026-05-10T12:00:00+00:00",
    }
    with respx.mock(assert_all_called=False) as router:
        router.get(
            f"{settings.gateway_url}/v1/audit/cost-by-agent"
        ).mock(return_value=httpx.Response(200, json=payload))
        r = await client.get("/api/cost-by-agent")
    assert r.status_code == 200
    assert r.json() == payload


async def test_proxy_cost_by_agent_gateway_down(
    client: AsyncClient, settings: Settings
):
    """A gateway connection failure becomes a 502 envelope."""
    with respx.mock(assert_all_called=False) as router:
        router.get(
            f"{settings.gateway_url}/v1/audit/cost-by-agent"
        ).mock(side_effect=httpx.ConnectError("boom"))
        r = await client.get("/api/cost-by-agent")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "UPSTREAM_UNREACHABLE"


async def test_proxy_anomalies_aggregates(
    client: AsyncClient, settings: Settings
):
    payload = {
        "detected_at": "2026-05-10T12:00:00+00:00",
        "window": "1h",
        "anomalies": [
            {
                "id": "anom_01TEST",
                "type": "cost_spike",
                "severity": "critical",
                "agent_id": "ag_a",
                "tenant_id": "default",
                "tool_id": None,
                "detected_at": "2026-05-10T12:00:00+00:00",
                "window_start": "2026-05-10T11:00:00+00:00",
                "window_end": "2026-05-10T12:00:00+00:00",
                "description": "agent ag_a cost spike",
                "metric_name": "cost_usd_per_minute",
                "metric_value": 5.0,
                "baseline_mean": 0.0001,
                "baseline_stddev": 0.0,
                "z_score": 100.0,
                "raw_data": {"baseline_samples": [0.0, 0.0]},
            }
        ],
        "total_anomalies": 1,
        "by_severity": {"critical": 1},
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.get(
            f"{settings.gateway_url}/v1/audit/anomalies"
        ).mock(return_value=httpx.Response(200, json=payload))
        r = await client.get("/api/anomalies?window=1h&min_severity=warning")
    assert r.status_code == 200
    assert r.json() == payload
    called_url = str(route.calls.last.request.url)
    assert "window=1h" in called_url
    assert "min_severity=warning" in called_url


async def test_proxy_anomalies_empty(
    client: AsyncClient, settings: Settings
):
    payload = {
        "detected_at": "2026-05-10T12:00:00+00:00",
        "window": "1h",
        "anomalies": [],
        "total_anomalies": 0,
        "by_severity": {},
    }
    with respx.mock(assert_all_called=False) as router:
        router.get(
            f"{settings.gateway_url}/v1/audit/anomalies"
        ).mock(return_value=httpx.Response(200, json=payload))
        r = await client.get("/api/anomalies")
    assert r.status_code == 200
    assert r.json()["total_anomalies"] == 0


async def test_proxy_anomalies_gateway_down(
    client: AsyncClient, settings: Settings
):
    with respx.mock(assert_all_called=False) as router:
        router.get(
            f"{settings.gateway_url}/v1/audit/anomalies"
        ).mock(side_effect=httpx.ConnectError("boom"))
        r = await client.get("/api/anomalies")
    assert r.status_code == 502


async def test_index_html_has_v14_panels(client: AsyncClient):
    """Overview shell ships the new cost-by-agent + anomalies markup."""
    r = await client.get("/")
    assert r.status_code == 200
    text = r.text
    assert 'id="cost-by-agent-body"' in text
    assert 'id="anomalies-list"' in text


async def test_static_app_js_has_v14_handlers(client: AsyncClient):
    r = await client.get("/static/app.js")
    assert r.status_code == 200
    js = r.text
    for needle in (
        "renderCostByAgent",
        "renderAnomalies",
        "refreshCostByAgent",
        "refreshAnomalies",
        "/api/cost-by-agent",
        "/api/anomalies",
    ):
        assert needle in js, f"missing {needle!r} in app.js"
