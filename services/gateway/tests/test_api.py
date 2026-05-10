# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Integration tests covering the full FastAPI surface."""

from __future__ import annotations

import respx
from httpx import Response


async def test_healthz(client) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "version": "0.1.0", "service": "gateway"}


async def test_auth_required(app_and_client, make_tool) -> None:
    app, client = app_and_client
    r = await client.post(
        "/v1/tools/register",
        json=make_tool(),
        headers={"Authorization": ""},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


async def test_auth_bad_scheme(app_and_client, make_tool) -> None:
    _, client = app_and_client
    r = await client.post(
        "/v1/tools/register",
        json=make_tool(),
        headers={"Authorization": "Basic abc"},
    )
    assert r.status_code == 401


async def test_register_list_get_delete(client, make_tool) -> None:
    body = make_tool()
    r = await client.post("/v1/tools/register", json=body)
    assert r.status_code == 201
    tool = r.json()
    assert tool["tool_id"] == "web.fetch"
    assert tool["idempotent"] is True
    assert "created_at" in tool

    # list
    r = await client.get("/v1/tools")
    assert r.status_code == 200
    assert {t["tool_id"] for t in r.json()["tools"]} == {"web.fetch"}

    # get
    r = await client.get("/v1/tools/web.fetch")
    assert r.status_code == 200
    assert r.json()["endpoint"] == body["endpoint"]

    # duplicate
    r = await client.post("/v1/tools/register", json=body)
    assert r.status_code == 400

    # delete
    r = await client.delete("/v1/tools/web.fetch")
    assert r.status_code == 204
    r = await client.get("/v1/tools/web.fetch")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TOOL_NOT_FOUND"


async def test_invoke_success(client, make_tool) -> None:
    tool = make_tool()
    await client.post("/v1/tools/register", json=tool)

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "hello", "status": 200})
        )
        r = await client.post(
            "/v1/invoke",
            json={
                "tool_id": "web.fetch",
                "arguments": {"url": "mock://demo"},
                "workspace_id": "ws_demo",
                "agent_id": "ag_demo",
            },
        )
        assert route.called

    assert r.status_code == 200
    body = r.json()
    assert body["tool_id"] == "web.fetch"
    assert body["result"] == {"content": "hello", "status": 200}
    assert body["cached"] is False
    assert body["duration_ms"] >= 0
    assert body["audit_id"].startswith("evt_")
    assert body["cost_estimate_usd"] > 0


async def test_invoke_cache_hit_then_miss_after_clear(client, make_tool) -> None:
    tool = make_tool(cache_ttl_seconds=300)
    await client.post("/v1/tools/register", json=tool)

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "v1"})
        )
        # First call: miss → backend hit
        r1 = await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "mock://x"}},
        )
        assert r1.status_code == 200
        assert r1.json()["cached"] is False
        assert route.call_count == 1

        # Second call: cache hit, no backend call
        r2 = await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "mock://x"}},
        )
        assert r2.status_code == 200
        assert r2.json()["cached"] is True
        assert r2.json()["cost_estimate_usd"] == 0.0
        assert route.call_count == 1

    # Clear cache, now hits backend again
    r = await client.delete("/v1/cache?tool_id=web.fetch")
    assert r.status_code == 204

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "v2"})
        )
        r3 = await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "mock://x"}},
        )
        assert r3.status_code == 200
        assert r3.json()["cached"] is False
        assert r3.json()["result"]["content"] == "v2"


async def test_invoke_cache_disabled_per_call(client, make_tool) -> None:
    tool = make_tool()
    await client.post("/v1/tools/register", json=tool)

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "x"})
        )
        for _ in range(2):
            r = await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": "mock://x"},
                    "cache": False,
                },
            )
            assert r.status_code == 200
            assert r.json()["cached"] is False
        assert route.call_count == 2


async def test_invoke_non_idempotent_never_cached(client, make_tool) -> None:
    tool = make_tool(tool_id="fs.write", idempotent=False, endpoint="http://mcp.test/invoke/write")
    await client.post("/v1/tools/register", json=tool)

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("http://mcp.test/invoke/write").mock(
            return_value=Response(200, json={"bytes_written": 4})
        )
        for _ in range(2):
            r = await client.post(
                "/v1/invoke",
                json={"tool_id": "fs.write", "arguments": {"path": "a", "content": "x"}},
            )
            assert r.status_code == 200
            assert r.json()["cached"] is False
        assert route.call_count == 2


async def test_invoke_unknown_tool(client) -> None:
    r = await client.post(
        "/v1/invoke",
        json={"tool_id": "does.not.exist", "arguments": {}},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TOOL_NOT_FOUND"


async def test_invoke_backend_error(client, make_tool) -> None:
    tool = make_tool()
    await client.post("/v1/tools/register", json=tool)

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(500, json={"error": "boom"})
        )
        r = await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "u"}},
        )
    assert r.status_code == 502
    body = r.json()
    assert body["error"]["code"] == "TOOL_INVOCATION_FAILED"
    assert "audit_id" in body["error"]["details"]

    # Audit log should contain the failed event
    r = await client.get("/v1/audit?tool_id=web.fetch")
    events = r.json()["events"]
    assert any(e["error"] is not None for e in events)


async def test_invoke_stdio_unsupported(client, make_tool) -> None:
    tool = make_tool()
    tool["transport"] = "stdio"
    tool["endpoint"] = "/usr/bin/some-mcp"
    await client.post("/v1/tools/register", json=tool)

    r = await client.post(
        "/v1/invoke",
        json={"tool_id": "web.fetch", "arguments": {"url": "u"}},
    )
    assert r.status_code == 501
    assert r.json()["error"]["code"] == "TRANSPORT_NOT_SUPPORTED"


async def test_invoke_outbound_bearer_auth(client, make_tool) -> None:
    tool = make_tool(auth_method="bearer", auth_config={"token": "secret-xyz"})
    await client.post("/v1/tools/register", json=tool)

    captured: dict = {}

    def _capture(request):
        captured["auth"] = request.headers.get("Authorization")
        return Response(200, json={"ok": True})

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(side_effect=_capture)
        r = await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "u"}},
        )
    assert r.status_code == 200
    assert captured["auth"] == "Bearer secret-xyz"


async def test_invoke_outbound_oauth2_mock(client, make_tool) -> None:
    tool = make_tool(auth_method="oauth2", auth_config={"mock_token": "oauth-mock"})
    await client.post("/v1/tools/register", json=tool)

    captured: dict = {}

    def _capture(request):
        captured["auth"] = request.headers.get("Authorization")
        return Response(200, json={"ok": True})

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(side_effect=_capture)
        r = await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "u"}},
        )
    assert r.status_code == 200
    assert captured["auth"] == "Bearer oauth-mock"


async def test_dry_run_miss_and_hit(client, make_tool) -> None:
    tool = make_tool()
    await client.post("/v1/tools/register", json=tool)

    # Initially no cache → would_invoke=True
    r = await client.post(
        "/v1/invoke/dry-run",
        json={"tool_id": "web.fetch", "arguments": {"url": "u"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["would_invoke"] is True
    assert body["cached_result"] is None
    assert body["estimated_cost_usd"] > 0

    # Populate cache via real invoke
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "cached"})
        )
        await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "u"}},
        )

    # Dry-run again → cache hit → would_invoke=False
    r = await client.post(
        "/v1/invoke/dry-run",
        json={"tool_id": "web.fetch", "arguments": {"url": "u"}},
    )
    body = r.json()
    assert body["would_invoke"] is False
    assert body["cached_result"] == {"content": "cached"}
    assert body["estimated_cost_usd"] == 0.0


async def test_dry_run_unknown_tool(client) -> None:
    r = await client.post(
        "/v1/invoke/dry-run",
        json={"tool_id": "does.not.exist", "arguments": {}},
    )
    assert r.status_code == 404


async def test_audit_query_filters(client, make_tool) -> None:
    await client.post("/v1/tools/register", json=make_tool())
    await client.post(
        "/v1/tools/register",
        json=make_tool(tool_id="web.search", endpoint="http://mcp.test/invoke/search"),
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "x"})
        )
        mock.post("http://mcp.test/invoke/search").mock(
            return_value=Response(200, json={"results": []})
        )
        await client.post(
            "/v1/invoke",
            json={
                "tool_id": "web.fetch",
                "arguments": {"url": "u1"},
                "workspace_id": "ws_a",
                "agent_id": "ag_1",
            },
        )
        await client.post(
            "/v1/invoke",
            json={
                "tool_id": "web.search",
                "arguments": {"query": "q"},
                "workspace_id": "ws_b",
                "agent_id": "ag_2",
            },
        )

    r = await client.get("/v1/audit?workspace_id=ws_a")
    events = r.json()["events"]
    assert len(events) == 1
    assert events[0]["tool_id"] == "web.fetch"

    r = await client.get("/v1/audit?tool_id=web.search")
    events = r.json()["events"]
    assert len(events) == 1
    assert events[0]["tool_id"] == "web.search"

    r = await client.get("/v1/audit?limit=1")
    assert len(r.json()["events"]) == 1


async def test_audit_query_invalid_since(client) -> None:
    r = await client.get("/v1/audit?since=not-a-date")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENTS"


async def test_audit_query_limit_bounds(client) -> None:
    r = await client.get("/v1/audit?limit=0")
    assert r.status_code == 422  # FastAPI Query validation
    r = await client.get("/v1/audit?limit=10000")
    assert r.status_code == 422


async def test_audit_stats(client, make_tool) -> None:
    await client.post("/v1/tools/register", json=make_tool())

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "x"})
        )
        # 1 miss, 1 hit
        await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "u"}, "workspace_id": "ws_a"},
        )
        await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "u"}, "workspace_id": "ws_a"},
        )

    r = await client.get("/v1/audit/stats?workspace_id=ws_a")
    body = r.json()["stats"]
    assert body["total_invocations"] == 2
    assert body["cached_count"] == 1
    assert body["error_count"] == 0
    # only the non-cached call carries cost
    assert body["total_cost_usd"] > 0
    assert body["by_tool"][0]["tool_id"] == "web.fetch"
    assert body["by_tool"][0]["count"] == 2


async def test_record_llm_audit_creates_event(client) -> None:
    """v1.2 — direct LLM audit endpoint synthesises audit_events row."""
    payload = {
        "tool_id": "llm.anthropic",
        "model": "claude-sonnet-4-5",
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": 0.0009,
        "duration_ms": 250,
        "workspace_id": "ws_llm",
        "agent_id": "agent_x",
        "finish_reason": "end_turn",
    }
    r = await client.post("/v1/audit/record-llm", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["audit_id"].startswith("evt_")

    # The new row shows up in the regular audit query.
    r2 = await client.get(
        "/v1/audit",
        params={"tool_id": "llm.anthropic", "workspace_id": "ws_llm"},
    )
    events = r2.json()["events"]
    assert len(events) == 1
    assert events[0]["tool_id"] == "llm.anthropic"
    assert events[0]["cost_estimate_usd"] == 0.0009
    assert events[0]["duration_ms"] == 250
    assert events[0]["agent_id"] == "agent_x"


async def test_record_llm_audit_minimal_payload(client) -> None:
    """All audit-record fields except tool_id+model are optional."""
    payload = {
        "tool_id": "llm.openai",
        "model": "gpt-5-mini",
    }
    r = await client.post("/v1/audit/record-llm", json=payload)
    assert r.status_code == 201
    audit_id = r.json()["audit_id"]
    assert audit_id.startswith("evt_")


async def test_record_llm_audit_rejects_extra_fields(client) -> None:
    """``extra='forbid'`` keeps the surface tight."""
    payload = {
        "tool_id": "llm.anthropic",
        "model": "claude-sonnet-4-5",
        "bogus_field": "nope",
    }
    r = await client.post("/v1/audit/record-llm", json=payload)
    assert r.status_code == 422


async def test_cache_stats_endpoint(client, make_tool) -> None:
    await client.post("/v1/tools/register", json=make_tool())

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "x"})
        )
        await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "u"}},
        )
        await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "u"}},
        )

    r = await client.get("/v1/cache/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["entries"] == 1
    assert body["hits"] == 1
    assert body["misses"] == 1
    assert body["size_bytes"] > 0


async def test_cache_clear_global(client, make_tool) -> None:
    await client.post("/v1/tools/register", json=make_tool())
    await client.post(
        "/v1/tools/register",
        json=make_tool(tool_id="web.search", endpoint="http://mcp.test/invoke/search"),
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "f"})
        )
        mock.post("http://mcp.test/invoke/search").mock(
            return_value=Response(200, json={"results": []})
        )
        await client.post("/v1/invoke", json={"tool_id": "web.fetch", "arguments": {"url": "u"}})
        await client.post("/v1/invoke", json={"tool_id": "web.search", "arguments": {"query": "q"}})

    r = await client.get("/v1/cache/stats")
    assert r.json()["entries"] == 2

    r = await client.delete("/v1/cache")
    assert r.status_code == 204

    r = await client.get("/v1/cache/stats")
    assert r.json()["entries"] == 0


async def test_cache_clear_unknown_tool(client) -> None:
    r = await client.delete("/v1/cache?tool_id=missing.tool")
    assert r.status_code == 404


async def test_register_validation_error(client) -> None:
    bad = {"tool_id": "x"}  # missing required fields
    r = await client.post("/v1/tools/register", json=bad)
    assert r.status_code == 422


async def test_invoke_default_pricing_unknown_tool_id(client, make_tool) -> None:
    tool = make_tool(tool_id="weird.tool", endpoint="http://mcp.test/invoke/weird")
    await client.post("/v1/tools/register", json=tool)

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/weird").mock(
            return_value=Response(200, json={"ok": True})
        )
        r = await client.post(
            "/v1/invoke",
            json={"tool_id": "weird.tool", "arguments": {}},
        )
    body = r.json()
    assert body["cost_estimate_usd"] == 0.0001  # default in pricing.py


async def test_auth_can_be_disabled(tmp_path, make_tool) -> None:
    """If ``inbound_auth_required=False`` no Authorization header is needed."""
    from httpx import ASGITransport, AsyncClient

    from plinth_gateway.api import create_app
    from plinth_gateway.settings import Settings

    s = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        log_format="console",
        inbound_auth_required=False,
    )
    app = create_app(s)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        async with app.router.lifespan_context(app):
            r = await c.post("/v1/tools/register", json=make_tool())
            assert r.status_code == 201


async def test_lifespan_cleans_expired_cache_on_startup(tmp_path) -> None:
    """Pre-seed an expired entry, restart app, ensure it's purged."""
    from datetime import datetime, timedelta, timezone

    from httpx import ASGITransport, AsyncClient

    from plinth_gateway.api import create_app
    from plinth_gateway.db import Database
    from plinth_gateway.settings import Settings

    s = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        log_format="console",
    )
    s.ensure_data_dir()

    # Seed an expired row directly
    db = Database(s.db_path)
    await db.connect()
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO cache_entries VALUES ('k1','t','h','{}',?,?,0)", (now, past)
    )
    await db.close()

    app = create_app(s)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer t"},
    ) as c:
        async with app.router.lifespan_context(app):
            r = await c.get("/v1/cache/stats")
            assert r.status_code == 200
            assert r.json()["entries"] == 0


async def test_health_no_auth_required(tmp_path) -> None:
    """``/healthz`` should not require auth."""
    from httpx import ASGITransport, AsyncClient

    from plinth_gateway.api import create_app
    from plinth_gateway.settings import Settings

    s = Settings(data_dir=tmp_path, log_level="WARNING")
    app = create_app(s)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        async with app.router.lifespan_context(app):
            r = await c.get("/healthz")
            assert r.status_code == 200


def test_main_invokes_uvicorn(monkeypatch) -> None:
    """``__main__.main`` should call uvicorn.run with the app path + bound port."""
    captured: dict = {}

    def fake_run(app_path, **kwargs):  # noqa: ANN001
        captured["app"] = app_path
        captured["kwargs"] = kwargs

    import plinth_gateway.__main__ as main_module

    monkeypatch.setattr(main_module.uvicorn, "run", fake_run)
    main_module.main()

    assert captured["app"] == "plinth_gateway.api:app"
    assert captured["kwargs"]["port"] == 7422


def test_logging_config_json_format() -> None:
    """Reach the JSON renderer branch in logging_config."""
    from plinth_gateway.logging_config import configure_logging, get_logger

    configure_logging("INFO", "json")
    log = get_logger("test")
    log.info("hello", k="v")  # smoke test, no error
