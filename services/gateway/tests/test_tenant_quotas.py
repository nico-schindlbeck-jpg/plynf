# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for tenant-level quota enforcement on ``/v1/invoke`` (v1.0)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from plinth_gateway.api import create_app
from plinth_gateway.db import Database
from plinth_gateway.settings import Settings
from plinth_gateway.tenant_quotas import (
    DEFAULT_MAX_COST_USD_DAY,
    DEFAULT_MAX_COST_USD_MONTH,
    DEFAULT_MAX_INVOCATIONS_PER_MINUTE,
    QuotaCache,
    TenantInvocationBucket,
    TenantQuotaEnforcer,
    TenantQuotas,
    cost_used_by_tenant,
    tenant_quotas_from_dict,
)


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers


_EVENT_COUNTER = 0


async def _insert_audit(
    db: Database,
    *,
    tenant_id: str,
    cost: float = 0.5,
    cached: bool = False,
    when: datetime | None = None,
) -> str:
    global _EVENT_COUNTER
    _EVENT_COUNTER += 1
    when = when or datetime.now(UTC)
    eid = f"evt_tq_{_EVENT_COUNTER}"
    await db.execute(
        """
        INSERT INTO audit_events
        (id, timestamp, tool_id, workspace_id, agent_id,
         arguments_hash, arguments_preview, result_hash,
         cached, duration_ms, cost_estimate_usd, error, tenant_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            eid,
            when.isoformat(),
            "web.fetch",
            None,
            "agt",
            "h" * 64,
            "{}",
            None,
            1 if cached else 0,
            10,
            cost,
            None,
            tenant_id,
        ),
    )
    return eid


def _sample_tool() -> dict:
    return {
        "tool_id": "web.fetch",
        "name": "Web Fetch",
        "description": "Fetch a URL",
        "transport": "http",
        "endpoint": "http://mcp.test/invoke/fetch",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "idempotent": True,
        "side_effects": "read",
        "cache_ttl_seconds": 300,
        "auth_method": "none",
        "auth_config": {},
    }


# ---------------------------------------------------------------------------
# TenantQuotaEnforcer — direct (no HTTP)


@pytest.mark.asyncio
async def test_enforcer_disabled_is_noop(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    db = Database(settings.db_path)
    await db.connect()
    try:
        cache = QuotaCache("", ttl_seconds=0)
        enforcer = TenantQuotaEnforcer(cache, db, enabled=False)
        # Even with cost-day vastly over the limit (we use defaults of $100),
        # the no-op should never raise.
        await _insert_audit(db, tenant_id="t1", cost=999_999.0)
        await enforcer.check_invoke("t1")
    finally:
        await db.close()


def _stub_cache_entry(cache: QuotaCache, quotas: TenantQuotas) -> None:
    """Pre-populate the cache so ``get`` returns the stubbed quotas."""

    cache._cache[quotas.tenant_id] = (quotas, cache._time())


@pytest.mark.asyncio
async def test_enforcer_blocks_when_cost_day_exceeded(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    db = Database(settings.db_path)
    await db.connect()
    try:
        cache = QuotaCache("", ttl_seconds=600)
        _stub_cache_entry(cache, TenantQuotas(
            tenant_id="t1",
            max_cost_usd_day=1.0,
            max_cost_usd_month=10.0,
            max_invocations_per_minute=1000,
        ))
        enforcer = TenantQuotaEnforcer(cache, db, enabled=True)

        # Pile up $2 in the last 24h.
        await _insert_audit(db, tenant_id="t1", cost=1.0)
        await _insert_audit(db, tenant_id="t1", cost=1.0)
        from plinth_gateway.tenant_quotas import QuotaExceeded

        with pytest.raises(QuotaExceeded) as excinfo:
            await enforcer.check_invoke("t1")
        assert excinfo.value.details["quota"] == "max_cost_usd_day"
        assert excinfo.value.details["tenant_id"] == "t1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_enforcer_blocks_when_invocations_per_minute_exceeded(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path)
    db = Database(settings.db_path)
    await db.connect()
    try:
        cache = QuotaCache("", ttl_seconds=600)
        _stub_cache_entry(cache, TenantQuotas(
            tenant_id="t1",
            max_cost_usd_day=1_000_000.0,
            max_cost_usd_month=10_000_000.0,
            max_invocations_per_minute=2,
        ))
        enforcer = TenantQuotaEnforcer(cache, db, enabled=True)
        await enforcer.record_invoke("t1")
        await enforcer.record_invoke("t1")
        from plinth_gateway.tenant_quotas import QuotaExceeded

        with pytest.raises(QuotaExceeded) as excinfo:
            await enforcer.check_invoke("t1")
        assert excinfo.value.details["quota"] == "max_invocations_per_minute"
        assert excinfo.value.details["limit"] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_different_tenants_have_isolated_quotas(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    db = Database(settings.db_path)
    await db.connect()
    try:
        cache = QuotaCache("", ttl_seconds=600)
        _stub_cache_entry(cache, TenantQuotas(
            tenant_id="t1",
            max_cost_usd_day=0.5,
            max_invocations_per_minute=1000,
        ))
        _stub_cache_entry(cache, TenantQuotas(
            tenant_id="t2",
            max_cost_usd_day=100.0,
            max_invocations_per_minute=1000,
        ))
        enforcer = TenantQuotaEnforcer(cache, db, enabled=True)

        # t1 is over its $0.50 cap.
        await _insert_audit(db, tenant_id="t1", cost=2.0)
        from plinth_gateway.tenant_quotas import QuotaExceeded

        with pytest.raises(QuotaExceeded):
            await enforcer.check_invoke("t1")

        # t2's $100 cap is untouched.
        await enforcer.check_invoke("t2")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_enforcer_blocks_when_cost_month_exceeded(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    db = Database(settings.db_path)
    await db.connect()
    try:
        cache = QuotaCache("", ttl_seconds=600)
        _stub_cache_entry(cache, TenantQuotas(
            tenant_id="t1",
            max_cost_usd_day=1_000_000.0,
            max_cost_usd_month=1.0,
            max_invocations_per_minute=1000,
        ))
        enforcer = TenantQuotaEnforcer(cache, db, enabled=True)

        # Put $2 in the last 24h — beats the month cap too.
        await _insert_audit(db, tenant_id="t1", cost=2.0)
        from plinth_gateway.tenant_quotas import QuotaExceeded

        with pytest.raises(QuotaExceeded) as excinfo:
            await enforcer.check_invoke("t1")
        # cost_day cap is huge so we fall through to cost_month.
        assert excinfo.value.details["quota"] == "max_cost_usd_month"
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# QuotaCache fallback behaviour


@pytest.mark.asyncio
async def test_quota_cache_uses_defaults_on_no_url() -> None:
    cache = QuotaCache("", ttl_seconds=0)
    q = await cache.get("t1")
    assert q.tenant_id == "t1"
    assert q.max_cost_usd_day == DEFAULT_MAX_COST_USD_DAY
    assert q.max_cost_usd_month == DEFAULT_MAX_COST_USD_MONTH
    assert q.max_invocations_per_minute == DEFAULT_MAX_INVOCATIONS_PER_MINUTE


@pytest.mark.asyncio
async def test_quota_cache_falls_back_on_identity_error() -> None:
    async def err_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = httpx.MockTransport(err_handler)
    client = httpx.AsyncClient(transport=transport)
    cache = QuotaCache(
        "http://identity.test",
        ttl_seconds=0,
        client=client,
    )
    q = await cache.get("t1")
    # Defaults — never raises.
    assert q.max_cost_usd_day == DEFAULT_MAX_COST_USD_DAY
    await client.aclose()


@pytest.mark.asyncio
async def test_quota_cache_reuses_within_ttl() -> None:
    calls = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"max_cost_usd_day": 7.5})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    cache = QuotaCache(
        "http://identity.test",
        ttl_seconds=60,
        client=client,
    )
    q1 = await cache.get("t1")
    q2 = await cache.get("t1")
    assert q1 == q2
    assert q1.max_cost_usd_day == 7.5
    assert len(calls) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_quota_cache_invalidate() -> None:
    calls = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"max_cost_usd_day": 5.0})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    cache = QuotaCache(
        "http://identity.test",
        ttl_seconds=600,
        client=client,
    )
    await cache.get("t1")
    cache.invalidate("t1")
    await cache.get("t1")
    assert len(calls) == 2
    await client.aclose()


# ---------------------------------------------------------------------------
# TenantInvocationBucket


@pytest.mark.asyncio
async def test_bucket_count_tracks_sliding_window() -> None:
    fake_t = [0.0]
    bucket = TenantInvocationBucket(time_fn=lambda: fake_t[0])
    fake_t[0] = 100.0
    await bucket.record("t1")
    fake_t[0] = 105.0
    await bucket.record("t1")
    assert await bucket.count_in_window("t1", window_seconds=60.0) == 2
    # Move time forward enough to expire the older record.
    fake_t[0] = 200.0
    assert await bucket.count_in_window("t1", window_seconds=60.0) == 0


# ---------------------------------------------------------------------------
# tenant_quotas_from_dict


def test_tenant_quotas_from_dict_uses_defaults() -> None:
    q = tenant_quotas_from_dict("t1", {"max_cost_usd_day": 50.0})
    assert q.tenant_id == "t1"
    assert q.max_cost_usd_day == 50.0
    # Unspecified field falls through to default.
    assert q.max_invocations_per_minute == DEFAULT_MAX_INVOCATIONS_PER_MINUTE


# ---------------------------------------------------------------------------
# /v1/invoke integration — full request path


async def _invoke_client_with_quotas(
    settings: Settings,
    *,
    quota_payload: dict | None = None,
):
    """Build a gateway client wired to a stub identity for quota fetches."""

    settings.quotas_enabled = True

    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=quota_payload or {})

    identity_transport = httpx.MockTransport(handler)
    identity_client = httpx.AsyncClient(transport=identity_transport)

    app = create_app(settings)
    transport = ASGITransport(app=app)
    client = AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    )
    return app, client, identity_client


@pytest.mark.asyncio
async def test_invoke_blocks_when_tenant_cost_day_exceeded(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
    )
    app, client, identity_client = await _invoke_client_with_quotas(
        settings,
        quota_payload={
            "max_cost_usd_day": 0.0006,
            "max_cost_usd_month": 1.0,
            "max_invocations_per_minute": 1000,
        },
    )
    async with client, app.router.lifespan_context(app):
        # Replace the cache's HTTP client with our stub.
        app.state.tenant_quotas._cache._client = identity_client
        app.state.tenant_quotas._cache._owns_client = False

        await client.post("/v1/tools/register", json=_sample_tool())

        with respx.mock(assert_all_called=False) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(
                return_value=Response(200, json={"content": "x"})
            )
            # web.fetch is $0.0005/call (cached defaults).
            # Two calls = $0.001 ≥ cap of $0.0006 → next call trips.
            for i in range(2):
                r = await client.post(
                    "/v1/invoke",
                    json={
                        "tool_id": "web.fetch",
                        "arguments": {"url": f"u{i}"},
                        "agent_id": "agt_a",
                        "cache": False,
                    },
                )
                assert r.status_code == 200, r.text

            r = await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": "u3"},
                    "agent_id": "agt_a",
                    "cache": False,
                },
            )
            assert r.status_code == 429
            body = r.json()
            assert body["error"]["code"] == "QUOTA_EXCEEDED"
            assert body["error"]["details"]["quota"] == "max_cost_usd_day"
    await identity_client.aclose()


@pytest.mark.asyncio
async def test_invoke_blocks_when_invocations_per_minute_exceeded(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
    )
    app, client, identity_client = await _invoke_client_with_quotas(
        settings,
        quota_payload={
            "max_cost_usd_day": 1_000.0,
            "max_cost_usd_month": 10_000.0,
            "max_invocations_per_minute": 2,
        },
    )
    async with client, app.router.lifespan_context(app):
        app.state.tenant_quotas._cache._client = identity_client
        app.state.tenant_quotas._cache._owns_client = False

        await client.post("/v1/tools/register", json=_sample_tool())

        with respx.mock(assert_all_called=False) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(
                return_value=Response(200, json={"content": "x"})
            )
            # First two go through, third gets 429.
            for i in range(2):
                r = await client.post(
                    "/v1/invoke",
                    json={
                        "tool_id": "web.fetch",
                        "arguments": {"url": f"u{i}"},
                        "agent_id": "agt_a",
                        "cache": False,
                    },
                )
                assert r.status_code == 200, r.text
            r = await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": "u_third"},
                    "agent_id": "agt_a",
                    "cache": False,
                },
            )
            assert r.status_code == 429
            body = r.json()
            assert body["error"]["code"] == "QUOTA_EXCEEDED"
            assert body["error"]["details"]["quota"] == "max_invocations_per_minute"
    await identity_client.aclose()


# ---------------------------------------------------------------------------
# cost_used_by_tenant


@pytest.mark.asyncio
async def test_cost_used_by_tenant_sums_audit_events(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    db = Database(settings.db_path)
    await db.connect()
    try:
        await _insert_audit(db, tenant_id="t1", cost=0.5)
        await _insert_audit(db, tenant_id="t1", cost=0.3)
        # Different tenant.
        await _insert_audit(db, tenant_id="t2", cost=99.0)
        used = await cost_used_by_tenant(db, "t1", hours=24)
        assert used == pytest.approx(0.8)
        used2 = await cost_used_by_tenant(db, "t2", hours=24)
        assert used2 == pytest.approx(99.0)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cost_used_by_tenant_excludes_cached(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    db = Database(settings.db_path)
    await db.connect()
    try:
        await _insert_audit(db, tenant_id="t1", cost=0.5, cached=False)
        await _insert_audit(db, tenant_id="t1", cost=0.5, cached=True)
        used = await cost_used_by_tenant(db, "t1", hours=24)
        # Cached call doesn't count.
        assert used == pytest.approx(0.5)
    finally:
        await db.close()
