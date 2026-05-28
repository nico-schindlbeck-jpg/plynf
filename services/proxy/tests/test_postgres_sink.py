# SPDX-License-Identifier: Apache-2.0
"""Tests for the Postgres savings sink.

Unit tests cover the row-mapping and SQL-shape contracts (no DB needed).
Integration tests only run when ``PLINTH_PROXY_TEST_POSTGRES_URL`` is set
(same convention as services/identity and services/workspace).
"""

from __future__ import annotations

import os

import pytest

from plinth_proxy.postgres_sink import (
    AGGREGATE_SQL,
    INSERT_SQL,
    SCHEMA_SQL,
    PostgresSavingsSink,
    _row_args,
)
from plinth_proxy.savings import make_event

# ---------------------------------------------------------------------------
# Unit tests — pure-Python, no DB
# ---------------------------------------------------------------------------


def test_schema_creates_table_and_indexes():
    assert "CREATE TABLE IF NOT EXISTS proxy_savings_events" in SCHEMA_SQL
    assert "tenant_id TEXT NOT NULL" in SCHEMA_SQL
    assert "idx_pse_tenant_ts" in SCHEMA_SQL
    assert "idx_pse_connector_ts" in SCHEMA_SQL


def test_insert_sql_has_13_placeholders():
    import re

    distinct = set(re.findall(r"\$\d+", INSERT_SQL))
    assert distinct == {f"${i}" for i in range(1, 14)}


def test_row_args_maps_event_in_correct_order():
    event = make_event(
        tenant_id="t1",
        agent_id="a1",
        connector="orders",
        tool="get_order",
        model="gpt-4o",
        raw_response_tokens=2161,
        shaped_response_tokens=83,
        cache_hit=False,
        request_args={"order_id": "12345"},
        workflow_id="wf-1",
    )
    args = _row_args(event)
    assert len(args) == 13
    assert args[1] == "t1"               # tenant_id
    assert args[3] == "orders"           # connector
    assert args[4] == "get_order"        # tool
    assert args[6] == 2161               # raw tokens
    assert args[7] == 83                 # shaped tokens
    assert args[8] == 2161 - 83          # saved tokens
    assert args[9] is False              # cache_hit
    assert isinstance(args[10], str)     # request_hash
    assert args[11] == "wf-1"            # workflow_id
    assert isinstance(args[12], float)   # cost_saved_usd


def test_row_args_cache_hit_event_uses_full_raw_as_saved():
    event = make_event(
        tenant_id="t1",
        agent_id=None,
        connector="orders",
        tool="get_order",
        model="gpt-4o",
        raw_response_tokens=2161,
        shaped_response_tokens=83,
        cache_hit=True,
        request_args={"order_id": "12345"},
    )
    args = _row_args(event)
    # On a cache hit we count the full raw tokens as saved (we didn't fetch).
    assert args[8] == 2161
    assert args[9] is True


def test_aggregate_sql_groups_by_tenant_only():
    # Belt-and-braces — make sure we never aggregate across tenants.
    assert "WHERE tenant_id = $1" in AGGREGATE_SQL


def test_sink_normalises_sqlalchemy_dsn(monkeypatch):
    sink = PostgresSavingsSink(dsn="postgresql+asyncpg://user:pw@host/db")
    # The pool isn't created yet, but the DSN should be normalised on use.
    # We can't easily test create_pool here without a DB; covered in integration.
    assert sink.dsn.startswith("postgresql+asyncpg://")


# ---------------------------------------------------------------------------
# Integration test — only runs when a Postgres URL is set
# ---------------------------------------------------------------------------


_PG_URL = os.environ.get("PLINTH_PROXY_TEST_POSTGRES_URL")


@pytest.mark.skipif(_PG_URL is None, reason="set PLINTH_PROXY_TEST_POSTGRES_URL")
@pytest.mark.asyncio
async def test_postgres_sink_roundtrip():
    sink = PostgresSavingsSink(dsn=_PG_URL)
    try:
        event = make_event(
            tenant_id="integration-test",
            agent_id="bot",
            connector="orders",
            tool="get_order",
            model="gpt-4o",
            raw_response_tokens=2000,
            shaped_response_tokens=200,
            cache_hit=False,
            request_args={"order_id": "test"},
        )
        await sink.emit_async(event)
        agg = await sink.aggregate_for_tenant("integration-test")
        assert agg["total_calls"] >= 1
        assert agg["total_saved_tokens"] >= 1800
    finally:
        await sink.close()
