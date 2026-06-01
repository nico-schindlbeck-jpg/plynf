# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""/v1/savings/summary reads through to Postgres when it's the sink.

The in-memory event mirror resets on restart; when a Postgres sink is the
system of record the summary must read aggregates back from it so the
dashboard's totals survive a proxy restart. These tests stub the sink's async
aggregate methods on a real PostgresSavingsSink instance, so no database is
touched — only the endpoint wiring is exercised.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.postgres_sink import PostgresSavingsSink
from plinth_proxy.settings import ProxySettings

_PG_SUMMARY = {
    "total_calls": 42,
    "total_raw_tokens": 1429,
    "total_shaped_tokens": 429,
    "total_saved_tokens": 1000,
    "savings_pct": 0.6997,
    "total_cost_saved_usd": 1.23,
    "cache_hit_rate": 0.5,
    "top_connectors_by_savings": [["salesforce", 800], ["slack", 200]],
}


def _app_with_pg_sink() -> tuple[object, PostgresSavingsSink]:
    app = create_app(ProxySettings(demo_mode=True))
    sink = PostgresSavingsSink(dsn="postgresql://unused/never-connected")
    app.state.plinth.sink = sink
    return app, sink


def test_summary_reads_through_to_postgres_all_tenants():
    app, sink = _app_with_pg_sink()

    async def fake_all():
        return dict(_PG_SUMMARY)

    sink.aggregate_all = fake_all  # type: ignore[method-assign]
    body = TestClient(app).get("/v1/savings/summary").json()
    assert body["total_calls"] == 42
    assert body["total_saved_tokens"] == 1000
    assert body["savings_pct"] == 0.6997


def test_summary_scopes_to_tenant_when_given():
    app, sink = _app_with_pg_sink()
    seen: dict[str, str] = {}

    async def fake_tenant(tenant_id):
        seen["tenant"] = tenant_id
        return {"total_calls": 7, "total_saved_tokens": 70}

    sink.aggregate_for_tenant = fake_tenant  # type: ignore[method-assign]
    body = TestClient(app).get("/v1/savings/summary?tenant=acme").json()
    assert seen["tenant"] == "acme"
    assert body["total_calls"] == 7


def test_summary_falls_back_to_memory_on_db_error():
    app, sink = _app_with_pg_sink()

    async def boom():
        raise RuntimeError("db unreachable")

    sink.aggregate_all = boom  # type: ignore[method-assign]
    # No events recorded → in-memory aggregate of an empty log.
    body = TestClient(app).get("/v1/savings/summary").json()
    assert body["total_calls"] == 0
    assert body["total_saved_tokens"] == 0


def test_summary_uses_memory_when_no_pg_sink():
    # Demo mode, no Postgres → in-memory mirror, never touches a sink.
    app = create_app(ProxySettings(demo_mode=True))
    body = TestClient(app).get("/v1/savings/summary").json()
    assert body["total_calls"] == 0
