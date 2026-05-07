# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Unit tests for :mod:`plinth_dashboard.overview`."""

from __future__ import annotations

import httpx
import pytest
import respx

from plinth_dashboard.overview import OverviewBuilder
from plinth_dashboard.settings import Settings


@pytest.fixture
def mocked(settings: Settings, workspace_factory, audit_stats_factory):
    """Build a respx router with all backends mocked happy-path."""
    router = respx.mock(assert_all_called=False)

    # workspace
    router.get(f"{settings.workspace_url}/healthz").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "version": "0.1.0", "service": "workspace"}
        )
    )
    ws_a = workspace_factory(ws_id="ws_a", name="research-1")
    ws_b = workspace_factory(ws_id="ws_b", name="pipeline-2")
    router.get(f"{settings.workspace_url}/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [ws_a, ws_b]})
    )
    router.get(f"{settings.workspace_url}/v1/tenants").mock(
        return_value=httpx.Response(
            200,
            json={"tenants": [{"id": "default", "workspace_count": 2}]},
        )
    )

    # gateway
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
            200, json={"hits": 38, "misses": 104, "entries": 67, "size_bytes": 412341}
        )
    )
    router.get(f"{settings.gateway_url}/v1/tools").mock(
        return_value=httpx.Response(
            200,
            json={
                "tools": [
                    {"tool_id": f"tool_{i}", "name": f"Tool {i}"} for i in range(6)
                ]
            },
        )
    )
    router.get(f"{settings.gateway_url}/v1/tenants").mock(
        return_value=httpx.Response(
            200,
            json={
                "tenants": [
                    {"id": "default", "audit_count": 142, "tool_count": 6},
                ]
            },
        )
    )

    # v0.4 — OTLP observability status + recent audit listing for the graph.
    router.get(f"{settings.gateway_url}/v1/observability/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "otlp_enabled": True,
                "otlp_endpoint": "http://localhost:4318",
                "otlp_service_name": "plinth-gateway",
                "events_emitted": 142,
                "last_emit_at": "2026-05-07T16:30:00Z",
                "flush_errors": 0,
            },
        )
    )
    router.get(f"{settings.gateway_url}/v1/audit").mock(
        return_value=httpx.Response(200, json={"events": []})
    )

    # mock-mcp healthz
    router.get(f"{settings.mock_mcp_url}/healthz").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "version": "0.1.0", "service": "mock-mcp"}
        )
    )

    # identity healthz (new in v0.3)
    router.get(f"{settings.identity_url}/healthz").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "version": "0.3.0", "service": "identity"}
        )
    )
    return router


# ---------------------------------------------------------------------------
# Happy-path aggregation


async def test_overview_happy_path(overview: OverviewBuilder, mocked):
    """All backends OK → full payload, partial=False, math correct."""
    with mocked:
        data = await overview.build()

    assert data["partial"] is False

    # Services pills
    assert data["services"]["workspace"]["status"] == "up"
    assert data["services"]["workspace"]["version"] == "0.1.0"
    assert data["services"]["workspace"]["url"] == "http://workspace.test"
    assert data["services"]["gateway"]["status"] == "up"
    assert data["services"]["mock_mcp"]["status"] == "up"

    # Workspaces shape: count + list
    assert data["workspaces"]["count"] == 2
    ids = [w["id"] for w in data["workspaces"]["list"]]
    assert ids == ["ws_a", "ws_b"]
    first = data["workspaces"]["list"][0]
    assert first["name"] == "research-1"
    assert "created_at" in first

    # Audit summary
    audit = data["audit"]
    assert audit["total_invocations"] == 142
    assert audit["cached_count"] == 38
    assert audit["error_count"] == 0
    assert audit["total_cost_usd"] == pytest.approx(0.0234, rel=1e-3)
    assert audit["by_tool"][0]["tool_id"] == "web.fetch"

    # Cache summary
    assert data["cache"]["hits"] == 38
    assert data["cache"]["misses"] == 104
    assert data["cache"]["entries"] == 67
    assert data["cache"]["size_bytes"] == 412341

    # Tools count
    assert data["tools"]["count"] == 6

    # Fetched timestamp present
    assert "fetched_at" in data


# ---------------------------------------------------------------------------
# Partial-failure scenarios


async def test_overview_partial_when_gateway_down(
    overview: OverviewBuilder, mocked, settings: Settings
):
    """If the gateway is unreachable, return what we have with partial=True."""
    with mocked:
        mocked.get(f"{settings.gateway_url}/healthz").mock(
            side_effect=httpx.ConnectError("gateway down")
        )
        mocked.get(f"{settings.gateway_url}/v1/audit/stats").mock(
            side_effect=httpx.ConnectError("gateway down")
        )
        mocked.get(f"{settings.gateway_url}/v1/cache/stats").mock(
            side_effect=httpx.ConnectError("gateway down")
        )
        mocked.get(f"{settings.gateway_url}/v1/tools").mock(
            side_effect=httpx.ConnectError("gateway down")
        )
        # v0.4 — also nuke the new endpoints.
        mocked.get(f"{settings.gateway_url}/v1/observability/status").mock(
            side_effect=httpx.ConnectError("gateway down")
        )
        mocked.get(f"{settings.gateway_url}/v1/audit").mock(
            side_effect=httpx.ConnectError("gateway down")
        )
        data = await overview.build()

    assert data["partial"] is True
    assert data["services"]["gateway"]["status"] == "down"
    # Workspaces still present
    assert data["workspaces"]["count"] == 2
    # Audit zeroed out
    assert data["audit"]["total_invocations"] == 0
    assert data["audit"]["total_cost_usd"] == 0.0
    assert data["cache"]["hits"] == 0
    assert data["tools"]["count"] == 0


async def test_overview_partial_when_workspace_down(
    overview: OverviewBuilder, mocked, settings: Settings
):
    """If the workspace listing fails, gateway data still flows through."""
    with mocked:
        mocked.get(f"{settings.workspace_url}/healthz").mock(
            side_effect=httpx.ConnectError("ws down")
        )
        mocked.get(f"{settings.workspace_url}/v1/workspaces").mock(
            side_effect=httpx.ConnectError("ws down")
        )
        data = await overview.build()

    assert data["partial"] is True
    assert data["workspaces"]["count"] == 0
    assert data["workspaces"]["list"] == []
    assert data["services"]["workspace"]["status"] == "down"
    # Gateway side intact
    assert data["audit"]["total_invocations"] == 142
    assert data["cache"]["hits"] == 38


async def test_overview_handles_4xx_from_upstream(
    overview: OverviewBuilder, settings: Settings
):
    """Unexpected non-2xx upstream responses are tolerated, not raised."""
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/healthz").mock(
            return_value=httpx.Response(
                200, json={"status": "ok", "version": "0.1.0", "service": "workspace"}
            )
        )
        router.get(f"{settings.workspace_url}/v1/workspaces").mock(
            return_value=httpx.Response(401, json={"error": {"code": "UNAUTHORIZED"}})
        )
        router.get(f"{settings.gateway_url}/healthz").mock(
            return_value=httpx.Response(503)
        )
        router.get(f"{settings.gateway_url}/v1/audit/stats").mock(
            return_value=httpx.Response(500, json={"error": "boom"})
        )
        router.get(f"{settings.gateway_url}/v1/cache/stats").mock(
            return_value=httpx.Response(500)
        )
        router.get(f"{settings.gateway_url}/v1/tools").mock(
            return_value=httpx.Response(500)
        )
        router.get(f"{settings.mock_mcp_url}/healthz").mock(
            return_value=httpx.Response(200, json={"version": "0.1.0"})
        )
        # New v0.3 endpoints — also error for this test scenario.
        router.get(f"{settings.workspace_url}/v1/tenants").mock(
            return_value=httpx.Response(500)
        )
        router.get(f"{settings.gateway_url}/v1/tenants").mock(
            return_value=httpx.Response(500)
        )
        router.get(f"{settings.identity_url}/healthz").mock(
            return_value=httpx.Response(503)
        )
        # v0.4 — observability + audit listing also error in this scenario.
        router.get(f"{settings.gateway_url}/v1/observability/status").mock(
            return_value=httpx.Response(500)
        )
        router.get(f"{settings.gateway_url}/v1/audit").mock(
            return_value=httpx.Response(500)
        )
        data = await overview.build()

    assert data["partial"] is True
    assert data["services"]["gateway"]["status"] == "down"
    assert data["workspaces"]["count"] == 0
    assert data["audit"]["total_invocations"] == 0


async def test_overview_owned_client_close(settings: Settings):
    """When we don't pass a client, ``aclose`` closes the owned one."""
    o = OverviewBuilder(settings)
    await o.aclose()
    # Idempotent: calling twice is safe.
    await o.aclose()


# ---------------------------------------------------------------------------
# v0.4 — observability + time-series enrichment
# ---------------------------------------------------------------------------


from datetime import datetime, timedelta, timezone  # noqa: E402

from plinth_dashboard.overview import (  # noqa: E402
    _build_timeseries,
    _summarise_observability,
)


async def test_overview_includes_observability_and_timeseries(
    overview: OverviewBuilder, mocked
):
    """The happy-path mock returns the new sections with sensible defaults."""
    with mocked:
        data = await overview.build()
    assert "observability" in data
    obs = data["observability"]
    assert obs["otlp_enabled"] is True
    assert obs["otlp_endpoint"] == "http://localhost:4318"
    assert obs["events_emitted"] == 142
    assert obs["flush_errors"] == 0
    # 5-minute counters derive from audit events; mock returns []. → zero.
    assert obs["events_emitted_5min"] == 0
    assert obs["errors_5min"] == 0

    # 60 buckets always, even when there's no data.
    assert "timeseries" in data
    series = data["timeseries"]["tool_calls_per_minute"]
    assert isinstance(series, list)
    assert len(series) == 60
    for bucket in series:
        assert "t" in bucket
        assert "count" in bucket
        assert "cost_usd" in bucket
        assert bucket["count"] == 0


async def test_overview_timeseries_buckets_are_60_when_no_events():
    """``_build_timeseries`` returns 60 zero-buckets for an empty input."""
    series = _build_timeseries([], minutes=60)
    assert len(series) == 60
    assert all(s["count"] == 0 for s in series)
    assert all(s["cost_usd"] == 0.0 for s in series)


async def test_overview_timeseries_aggregates_events_by_minute():
    """Events in the same minute roll up; later events go to later buckets."""
    now = datetime(2026, 5, 7, 16, 30, 0, tzinfo=timezone.utc)
    events = [
        # 3 events in the bucket 1 minute ago.
        {
            "timestamp": (now - timedelta(seconds=70)).isoformat().replace("+00:00", "Z"),
            "cost_estimate_usd": 0.001,
        },
        {
            "timestamp": (now - timedelta(seconds=80)).isoformat().replace("+00:00", "Z"),
            "cost_estimate_usd": 0.002,
        },
        {
            "timestamp": (now - timedelta(seconds=90)).isoformat().replace("+00:00", "Z"),
            "cost_estimate_usd": 0.003,
        },
        # 1 event in the bucket 2 minutes ago.
        {
            "timestamp": (now - timedelta(seconds=130)).isoformat().replace("+00:00", "Z"),
            "cost_estimate_usd": 0.0005,
        },
    ]
    series = _build_timeseries(events, now=now, minutes=60)
    assert len(series) == 60
    # The current-minute bucket (16:30) gets nothing — all events are older.
    assert series[-1]["count"] == 0
    # 1 minute ago (16:29) — also nothing in this fixture.
    assert series[-2]["count"] == 0
    # 2 minutes ago (16:28) gets the three -70/-80/-90s events.
    assert series[-3]["count"] == 3
    assert series[-3]["cost_usd"] == pytest.approx(0.006)
    # 3 minutes ago (16:27) gets the single -130s event.
    assert series[-4]["count"] == 1
    assert series[-4]["cost_usd"] == pytest.approx(0.0005)


async def test_overview_timeseries_drops_events_outside_window():
    """Events older than ``minutes`` are not counted in any bucket."""
    now = datetime(2026, 5, 7, 16, 30, 0, tzinfo=timezone.utc)
    events = [
        {
            "timestamp": (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            "cost_estimate_usd": 0.5,
        },
    ]
    series = _build_timeseries(events, now=now, minutes=60)
    assert all(s["count"] == 0 for s in series)


async def test_overview_observability_5min_counters():
    """``events_emitted_5min`` and ``errors_5min`` count the right window."""
    now = datetime(2026, 5, 7, 16, 30, 0, tzinfo=timezone.utc)
    events = [
        {"timestamp": (now - timedelta(minutes=2)).isoformat()},  # in window
        {
            "timestamp": (now - timedelta(minutes=4)).isoformat(),
            "error": "boom",
        },  # in window, error
        {"timestamp": (now - timedelta(minutes=10)).isoformat()},  # out of window
    ]
    obs = _summarise_observability(
        {"otlp_enabled": True, "otlp_endpoint": "x", "events_emitted": 99},
        events,
        now=now,
    )
    assert obs["events_emitted_5min"] == 2
    assert obs["errors_5min"] == 1
    assert obs["otlp_enabled"] is True


async def test_overview_observability_status_404_falls_back(
    overview: OverviewBuilder, mocked, settings: Settings
):
    """Older gateway returning 404 on /v1/observability/status → defaults."""
    with mocked:
        mocked.get(f"{settings.gateway_url}/v1/observability/status").mock(
            return_value=httpx.Response(404)
        )
        data = await overview.build()
    obs = data["observability"]
    assert obs["otlp_enabled"] is False
    assert obs["otlp_endpoint"] is None
    assert obs["events_emitted"] == 0
    assert obs["flush_errors"] == 0
    # Timeseries still renders 60 buckets.
    assert len(data["timeseries"]["tool_calls_per_minute"]) == 60


async def test_overview_audit_listing_failure_does_not_break_overview(
    overview: OverviewBuilder, mocked, settings: Settings
):
    """Failure on /v1/audit returns empty events; timeseries stays valid."""
    with mocked:
        mocked.get(f"{settings.gateway_url}/v1/audit").mock(
            side_effect=httpx.ConnectError("audit down")
        )
        data = await overview.build()
    series = data["timeseries"]["tool_calls_per_minute"]
    assert len(series) == 60
    assert all(b["count"] == 0 for b in series)
    # Other observability values still flow through from the status endpoint.
    assert data["observability"]["otlp_enabled"] is True


# ---------------------------------------------------------------------------
# v0.5 — dead-letter enrichment
# ---------------------------------------------------------------------------


async def test_overview_includes_deadletters_section_when_no_dlq(
    overview: OverviewBuilder, mocked, settings: Settings
):
    """Empty DLQ across all workspaces → ``deadletters`` is an empty list."""
    with mocked:
        mocked.get(f"{settings.workspace_url}/v1/workspaces/ws_a/channels").mock(
            return_value=httpx.Response(200, json={"channels": []})
        )
        mocked.get(f"{settings.workspace_url}/v1/workspaces/ws_b/channels").mock(
            return_value=httpx.Response(200, json={"channels": []})
        )
        data = await overview.build()
    assert "deadletters" in data
    assert data["deadletters"] == []


async def test_overview_deadletters_lists_only_non_empty_dlqs(
    overview: OverviewBuilder, mocked, settings: Settings
):
    """Channels with non-zero DLQ counts surface in the dashboard payload."""
    ws_a = settings.workspace_url
    with mocked:
        # ws_a has two channels, only one with DLQ entries.
        mocked.get(f"{ws_a}/v1/workspaces/ws_a/channels").mock(
            return_value=httpx.Response(
                200,
                json={
                    "channels": [
                        {
                            "name": "research-out",
                            "workspace_id": "ws_a",
                            "message_count": 5,
                            "created_at": "2026-05-07T00:00:00Z",
                            "last_send_at": None,
                            "last_receive_at": None,
                        },
                        {
                            "name": "writer-out",
                            "workspace_id": "ws_a",
                            "message_count": 0,
                            "created_at": "2026-05-07T00:00:00Z",
                            "last_send_at": None,
                            "last_receive_at": None,
                        },
                    ]
                },
            )
        )
        mocked.get(f"{ws_a}/v1/workspaces/ws_b/channels").mock(
            return_value=httpx.Response(200, json={"channels": []})
        )
        # research-out DLQ has 3 messages, writer-out has none.
        mocked.get(
            f"{ws_a}/v1/workspaces/ws_a/channels/research-out/deadletter"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "id": f"msg_dlq_{i}",
                            "channel": "research-out.deadletter",
                            "workspace_id": "ws_a",
                            "seq": i,
                            "payload": {"topic": "x"},
                            "sender": None,
                            "type": None,
                            "correlation_id": None,
                            "headers": {"x-original-channel": "research-out"},
                            "sent_at": "2026-05-07T00:00:00Z",
                            "delivered_at": None,
                        }
                        for i in range(1, 4)
                    ]
                },
            )
        )
        mocked.get(
            f"{ws_a}/v1/workspaces/ws_a/channels/writer-out/deadletter"
        ).mock(return_value=httpx.Response(200, json={"messages": []}))
        data = await overview.build()
    assert data["deadletters"] == [
        {
            "workspace_id": "ws_a",
            "channel": "research-out",
            "deadletter_count": 3,
        }
    ]


async def test_overview_deadletters_filters_dlq_subchannels(
    overview: OverviewBuilder, mocked, settings: Settings
):
    """``.deadletter`` channels in the listing aren't probed (no recursion)."""
    ws_a = settings.workspace_url
    with mocked:
        mocked.get(f"{ws_a}/v1/workspaces/ws_a/channels").mock(
            return_value=httpx.Response(
                200,
                json={
                    "channels": [
                        {
                            "name": "research-out",
                            "workspace_id": "ws_a",
                            "message_count": 0,
                            "created_at": "2026-05-07T00:00:00Z",
                            "last_send_at": None,
                            "last_receive_at": None,
                        },
                        # Deliberately included to test we filter it out.
                        {
                            "name": "research-out.deadletter",
                            "workspace_id": "ws_a",
                            "message_count": 1,
                            "created_at": "2026-05-07T00:00:00Z",
                            "last_send_at": None,
                            "last_receive_at": None,
                        },
                    ]
                },
            )
        )
        mocked.get(f"{ws_a}/v1/workspaces/ws_b/channels").mock(
            return_value=httpx.Response(200, json={"channels": []})
        )
        # Only the main channel's DLQ is queried.
        mocked.get(
            f"{ws_a}/v1/workspaces/ws_a/channels/research-out/deadletter"
        ).mock(return_value=httpx.Response(200, json={"messages": []}))
        data = await overview.build()
    assert data["deadletters"] == []


async def test_overview_observability_status_disabled_payload(
    overview: OverviewBuilder, settings: Settings, workspace_factory, audit_stats_factory
):
    """When the gateway reports OTLP disabled, the dashboard mirrors that."""
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/healthz").mock(
            return_value=httpx.Response(200, json={"version": "0.1.0"})
        )
        router.get(f"{settings.workspace_url}/v1/workspaces").mock(
            return_value=httpx.Response(
                200, json={"workspaces": [workspace_factory(ws_id="ws_a")]}
            )
        )
        router.get(f"{settings.workspace_url}/v1/tenants").mock(
            return_value=httpx.Response(200, json={"tenants": []})
        )
        router.get(f"{settings.gateway_url}/healthz").mock(
            return_value=httpx.Response(200, json={"version": "0.1.0"})
        )
        router.get(f"{settings.gateway_url}/v1/audit/stats").mock(
            return_value=httpx.Response(200, json=audit_stats_factory())
        )
        router.get(f"{settings.gateway_url}/v1/cache/stats").mock(
            return_value=httpx.Response(
                200, json={"hits": 0, "misses": 0, "entries": 0, "size_bytes": 0}
            )
        )
        router.get(f"{settings.gateway_url}/v1/tools").mock(
            return_value=httpx.Response(200, json={"tools": []})
        )
        router.get(f"{settings.gateway_url}/v1/tenants").mock(
            return_value=httpx.Response(200, json={"tenants": []})
        )
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
        router.get(f"{settings.mock_mcp_url}/healthz").mock(
            return_value=httpx.Response(200, json={"version": "0.1.0"})
        )
        router.get(f"{settings.identity_url}/healthz").mock(
            return_value=httpx.Response(200, json={"version": "0.3.0"})
        )
        data = await overview.build()
    assert data["observability"]["otlp_enabled"] is False
    assert data["observability"]["otlp_endpoint"] is None
