# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the agent decorator and ``AgentContext``."""

from __future__ import annotations

import httpx
import respx

from plinth import AgentContext, Plinth

from .conftest import (
    make_invoke_response,
    make_kv_entry,
    make_workspace,
)


def test_agent_decorator_provides_context(
    client: Plinth,
    workspace_mock: respx.MockRouter,
    gateway_mock: respx.MockRouter,
):
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(
            200,
            json={"workspaces": [make_workspace(name="my-task")]},
        )
    )

    captured: dict = {}

    @client.agent(workspace="my-task")
    def my_agent(ctx, topic: str) -> str:
        captured["ctx"] = ctx
        captured["topic"] = topic
        return f"done: {topic}"

    out = my_agent(topic="renewable energy")

    assert out == "done: renewable energy"
    ctx: AgentContext = captured["ctx"]
    assert ctx.client is client
    assert ctx.workspace.name == "my-task"
    assert ctx.tools is not None  # the scoped wrapper


def test_agent_tools_invocation_auto_tags_workspace_id(
    client: Plinth,
    workspace_mock: respx.MockRouter,
    gateway_mock: respx.MockRouter,
):
    ws_payload = make_workspace(ws_id="ws_TAGGED", name="my-task")
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [ws_payload]})
    )

    captured_bodies: list[bytes] = []

    def handler(request):
        captured_bodies.append(request.read())
        return httpx.Response(200, json=make_invoke_response())

    gateway_mock.post("/v1/invoke").mock(side_effect=handler)

    @client.agent(workspace="my-task", agent_id="agent-007")
    def my_agent(ctx, query: str):
        return ctx.tools.invoke("web.search", {"query": query})

    my_agent(query="x")

    assert b"ws_TAGGED" in captured_bodies[0]
    assert b"agent-007" in captured_bodies[0]


def test_agent_can_write_to_workspace(
    client: Plinth,
    workspace_mock: respx.MockRouter,
    gateway_mock: respx.MockRouter,
):
    ws_payload = make_workspace(name="my-task")
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [ws_payload]})
    )
    workspace_mock.put(f"/v1/workspaces/{ws_payload['id']}/kv/topic").mock(
        return_value=httpx.Response(200, json=make_kv_entry(key="topic", value="x", version=1))
    )

    @client.agent(workspace="my-task")
    def write_topic(ctx, value):
        return ctx.workspace.kv.set("topic", value)

    entry = write_topic(value="x")
    assert entry.value == "x"


def test_scoped_tools_forwards_unrelated_methods(
    client: Plinth,
    workspace_mock: respx.MockRouter,
    gateway_mock: respx.MockRouter,
):
    ws_payload = make_workspace(name="my-task")
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [ws_payload]})
    )
    gateway_mock.get("/v1/cache/stats").mock(
        return_value=httpx.Response(200, json={"hits": 1, "misses": 2, "size_bytes": 0})
    )

    @client.agent(workspace="my-task")
    def look(ctx):
        return ctx.tools.cache_stats()

    out = look()
    assert out["hits"] == 1


def test_agent_audit_auto_includes_workspace_id(
    client: Plinth,
    workspace_mock: respx.MockRouter,
    gateway_mock: respx.MockRouter,
):
    ws_payload = make_workspace(ws_id="ws_AUDIT", name="my-task")
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [ws_payload]})
    )

    captured: dict = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"events": []})

    gateway_mock.get("/v1/audit").mock(side_effect=handler)

    @client.agent(workspace="my-task")
    def query(ctx):
        return ctx.tools.audit()

    query()
    assert captured["params"]["workspace_id"] == "ws_AUDIT"


def test_agent_decorator_preserves_function_name(client: Plinth):
    @client.agent(workspace="x")
    def my_named_agent(ctx, q):
        return q

    assert my_named_agent.__name__ == "my_named_agent"


def test_agent_dry_run_auto_tags(
    client: Plinth,
    workspace_mock: respx.MockRouter,
    gateway_mock: respx.MockRouter,
):
    ws_payload = make_workspace(ws_id="ws_DRY", name="my-task")
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [ws_payload]})
    )

    captured: dict = {}

    def handler(request):
        captured["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "tool_id": "web.fetch",
                "arguments": {"url": "x"},
                "would_invoke": True,
                "cached_result": None,
                "estimated_cost_usd": 0.0,
                "estimated_duration_ms": 0,
            },
        )

    gateway_mock.post("/v1/invoke/dry-run").mock(side_effect=handler)

    @client.agent(workspace="my-task", agent_id="agent-99")
    def look(ctx):
        return ctx.tools.dry_run("web.fetch", {"url": "x"})

    look()
    assert b"ws_DRY" in captured["body"]
    assert b"agent-99" in captured["body"]
