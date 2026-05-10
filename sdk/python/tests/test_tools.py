# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the gateway client (`plinth.tools`)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from plinth import (
    InvalidArguments,
    Plinth,
    ToolNotFound,
    ToolRegistration,
)
from plinth.tools import _parse_since

from .conftest import (
    error_envelope,
    make_audit_event,
    make_invoke_response,
    make_tool,
)

# ---------------------------------------------------------------------------
# invoke
# ---------------------------------------------------------------------------


def test_tools_invoke_returns_parsed_response(client: Plinth, gateway_mock: respx.MockRouter):
    gateway_mock.post("/v1/invoke").mock(
        return_value=httpx.Response(
            200,
            json=make_invoke_response(
                tool_id="web.fetch",
                arguments={"url": "mock://hello"},
                result={"content": "hi", "status": 200},
            ),
        )
    )

    resp = client.tools.invoke("web.fetch", {"url": "mock://hello"})

    assert resp.tool_id == "web.fetch"
    assert resp.result["content"] == "hi"
    assert resp.cached is False


def test_tools_invoke_passes_cache_flag(client: Plinth, gateway_mock: respx.MockRouter):
    captured: dict = {}

    def handler(request):
        captured["body"] = request.read()
        return httpx.Response(200, json=make_invoke_response())

    gateway_mock.post("/v1/invoke").mock(side_effect=handler)
    client.tools.invoke("web.search", {"query": "x"}, cache=False)

    assert b'"cache":false' in captured["body"]


def test_tools_invoke_404_raises_toolnotfound(client: Plinth, gateway_mock: respx.MockRouter):
    gateway_mock.post("/v1/invoke").mock(
        return_value=httpx.Response(404, json=error_envelope("TOOL_NOT_FOUND", "not registered"))
    )

    with pytest.raises(ToolNotFound) as info:
        client.tools.invoke("nonexistent", {})

    assert info.value.code == "TOOL_NOT_FOUND"


def test_tools_invoke_with_workspace_and_agent(client: Plinth, gateway_mock: respx.MockRouter):
    captured: dict = {}

    def handler(request):
        captured["body"] = request.read()
        return httpx.Response(200, json=make_invoke_response())

    gateway_mock.post("/v1/invoke").mock(side_effect=handler)
    client.tools.invoke(
        "web.fetch",
        {"url": "x"},
        workspace_id="ws_X",
        agent_id="agent_42",
        idempotency_key="key-1",
    )

    body = captured["body"]
    assert b"ws_X" in body
    assert b"agent_42" in body
    assert b"key-1" in body


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------


def test_tools_dry_run(client: Plinth, gateway_mock: respx.MockRouter):
    gateway_mock.post("/v1/invoke/dry-run").mock(
        return_value=httpx.Response(
            200,
            json={
                "tool_id": "web.fetch",
                "arguments": {"url": "x"},
                "would_invoke": False,
                "cached_result": {"content": "cached"},
                "estimated_cost_usd": 0.0,
                "estimated_duration_ms": 5,
            },
        )
    )

    resp = client.tools.dry_run("web.fetch", {"url": "x"})

    assert resp.would_invoke is False
    assert resp.cached_result == {"content": "cached"}


# ---------------------------------------------------------------------------
# Register / list / get / deregister
# ---------------------------------------------------------------------------


def test_tools_register_with_dict(client: Plinth, gateway_mock: respx.MockRouter):
    gateway_mock.post("/v1/tools/register").mock(
        return_value=httpx.Response(201, json=make_tool(tool_id="my.tool"))
    )

    out = client.tools.register(
        {
            "tool_id": "my.tool",
            "name": "my.tool",
            "description": "desc",
            "transport": "http",
            "endpoint": "http://example/",
            "input_schema": {},
            "output_schema": {},
        }
    )

    assert out.tool_id == "my.tool"


def test_tools_register_with_model(client: Plinth, gateway_mock: respx.MockRouter):
    reg = ToolRegistration(
        tool_id="my.tool",
        name="my.tool",
        description="desc",
        transport="http",
        endpoint="http://example/",
    )
    gateway_mock.post("/v1/tools/register").mock(
        return_value=httpx.Response(201, json=make_tool(tool_id="my.tool"))
    )

    out = client.tools.register(reg)
    assert out.tool_id == "my.tool"


def test_tools_list(client: Plinth, gateway_mock: respx.MockRouter):
    gateway_mock.get("/v1/tools").mock(
        return_value=httpx.Response(
            200, json={"tools": [make_tool(tool_id="a"), make_tool(tool_id="b")]}
        )
    )

    tools = client.tools.list()

    assert {t.tool_id for t in tools} == {"a", "b"}


def test_tools_get(client: Plinth, gateway_mock: respx.MockRouter):
    gateway_mock.get("/v1/tools/web.fetch").mock(
        return_value=httpx.Response(200, json=make_tool(tool_id="web.fetch"))
    )

    tool = client.tools.get("web.fetch")
    assert tool.tool_id == "web.fetch"


def test_tools_deregister(client: Plinth, gateway_mock: respx.MockRouter):
    route = gateway_mock.delete("/v1/tools/web.fetch").mock(return_value=httpx.Response(204))

    client.tools.deregister("web.fetch")
    assert route.called


# ---------------------------------------------------------------------------
# Audit / stats / cache
# ---------------------------------------------------------------------------


def test_tools_audit_with_relative_since(client: Plinth, gateway_mock: respx.MockRouter):
    captured: dict = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"events": [make_audit_event(workspace_id="ws_X")]})

    gateway_mock.get("/v1/audit").mock(side_effect=handler)
    out = client.tools.audit(workspace_id="ws_X", since="1h", limit=50)

    assert out[0].workspace_id == "ws_X"
    assert captured["params"]["workspace_id"] == "ws_X"
    assert captured["params"]["limit"] == "50"
    # 'since' should have been converted to an ISO string ending in +00:00.
    assert captured["params"]["since"].endswith("+00:00")


def test_tools_audit_with_iso_since(client: Plinth, gateway_mock: respx.MockRouter):
    captured: dict = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"events": []})

    gateway_mock.get("/v1/audit").mock(side_effect=handler)
    client.tools.audit(since="2026-01-01T00:00:00Z")
    assert captured["params"]["since"] == "2026-01-01T00:00:00+00:00"


def test_tools_audit_invalid_since_raises_invalidarguments(client: Plinth):
    with pytest.raises(InvalidArguments):
        client.tools.audit(since="not-a-real-thing")


def test_tools_stats_unwraps_envelope(client: Plinth, gateway_mock: respx.MockRouter):
    gateway_mock.get("/v1/audit/stats").mock(
        return_value=httpx.Response(200, json={"stats": {"total_calls": 17}})
    )

    out = client.tools.stats(workspace_id="ws_X")
    assert out["total_calls"] == 17


def test_tools_stats_passes_through_flat(client: Plinth, gateway_mock: respx.MockRouter):
    gateway_mock.get("/v1/audit/stats").mock(
        return_value=httpx.Response(200, json={"total_calls": 17})
    )

    out = client.tools.stats()
    assert out["total_calls"] == 17


def test_tools_cache_stats_and_clear(client: Plinth, gateway_mock: respx.MockRouter):
    gateway_mock.get("/v1/cache/stats").mock(
        return_value=httpx.Response(200, json={"hits": 10, "misses": 5, "size_bytes": 1024})
    )
    out = client.tools.cache_stats()
    assert out["hits"] == 10

    route = gateway_mock.delete("/v1/cache").mock(return_value=httpx.Response(204))
    client.tools.clear_cache(tool_id="web.fetch")
    assert route.called


# ---------------------------------------------------------------------------
# _parse_since unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["1h", "30m", "7d", "45s", "2w"],
)
def test_parse_since_relative_durations(value):
    out = _parse_since(value)
    assert out is not None
    # Parses back as a real datetime.
    parsed = datetime.fromisoformat(out)
    assert parsed.tzinfo is not None


def test_parse_since_datetime_naive_is_treated_utc():
    out = _parse_since(datetime(2026, 1, 1, 0, 0, 0))
    assert out == "2026-01-01T00:00:00+00:00"


def test_parse_since_datetime_with_tz_is_normalised_utc():
    out = _parse_since(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))  # noqa: UP017
    assert out == "2026-01-01T00:00:00+00:00"


def test_parse_since_none_returns_none():
    assert _parse_since(None) is None


def test_parse_since_invalid_raises():
    with pytest.raises(ValueError):
        _parse_since("garbage")


# ---------------------------------------------------------------------------
# v1.4 — cost-by-agent + anomalies SDK accessors
# ---------------------------------------------------------------------------


def test_cost_by_agent_round_trip(client: Plinth, gateway_mock: respx.MockRouter):
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
                ],
            }
        ],
        "total_agents": 1,
        "total_cost_usd": 0.42,
        "fetched_at": "2026-05-10T12:00:00+00:00",
    }
    route = gateway_mock.get("/v1/audit/cost-by-agent").mock(
        return_value=httpx.Response(200, json=payload),
    )
    report = client.gateway.cost_by_agent(window="24h", top=5)
    assert report.window == "24h"
    assert report.total_agents == 1
    assert report.agents[0].agent_id == "ag_a"
    assert report.agents[0].top_tools[0].tool_id == "web.fetch"
    # Forwarded query params
    called_url = str(route.calls.last.request.url)
    assert "window=24h" in called_url
    assert "top=5" in called_url


def test_cost_by_agent_with_tenant(client: Plinth, gateway_mock: respx.MockRouter):
    payload = {
        "window": "1h",
        "window_start": "2026-05-10T11:00:00+00:00",
        "window_end": "2026-05-10T12:00:00+00:00",
        "agents": [],
        "total_agents": 0,
        "total_cost_usd": 0.0,
        "fetched_at": "2026-05-10T12:00:00+00:00",
    }
    route = gateway_mock.get("/v1/audit/cost-by-agent").mock(
        return_value=httpx.Response(200, json=payload),
    )
    client.gateway.cost_by_agent(window="1h", tenant_id="tenant-x")
    called_url = str(route.calls.last.request.url)
    assert "tenant_id=tenant-x" in called_url


def test_anomalies_round_trip(client: Plinth, gateway_mock: respx.MockRouter):
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
    route = gateway_mock.get("/v1/audit/anomalies").mock(
        return_value=httpx.Response(200, json=payload),
    )
    report = client.gateway.anomalies(window="1h", min_severity="warning")
    assert report.total_anomalies == 1
    assert report.anomalies[0].type == "cost_spike"
    assert report.anomalies[0].severity == "critical"
    assert report.by_severity["critical"] == 1
    called_url = str(route.calls.last.request.url)
    assert "window=1h" in called_url
    assert "min_severity=warning" in called_url


def test_anomalies_with_filters(client: Plinth, gateway_mock: respx.MockRouter):
    payload = {
        "detected_at": "2026-05-10T12:00:00+00:00",
        "window": "1h",
        "anomalies": [],
        "total_anomalies": 0,
        "by_severity": {},
    }
    route = gateway_mock.get("/v1/audit/anomalies").mock(
        return_value=httpx.Response(200, json=payload),
    )
    client.gateway.anomalies(
        window="1h",
        min_severity="info",
        type="cost_spike",
        agent_id="ag_a",
    )
    called_url = str(route.calls.last.request.url)
    assert "type=cost_spike" in called_url
    assert "agent_id=ag_a" in called_url


def test_cost_by_agent_aliased_via_gateway(
    client: Plinth, gateway_mock: respx.MockRouter
):
    """``client.gateway`` is the v0.5 alias of ``client.tools`` — both work."""
    payload = {
        "window": "24h",
        "window_start": "2026-05-09T12:00:00+00:00",
        "window_end": "2026-05-10T12:00:00+00:00",
        "agents": [],
        "total_agents": 0,
        "total_cost_usd": 0.0,
        "fetched_at": "2026-05-10T12:00:00+00:00",
    }
    gateway_mock.get("/v1/audit/cost-by-agent").mock(
        return_value=httpx.Response(200, json=payload),
    )
    via_tools = client.tools.cost_by_agent(window="24h")
    via_gateway = client.gateway.cost_by_agent(window="24h")
    assert via_tools.window == via_gateway.window == "24h"
