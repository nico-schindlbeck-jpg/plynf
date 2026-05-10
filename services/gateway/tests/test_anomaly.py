# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.4 audit-log anomaly detector + endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plinth_gateway import anomaly
from plinth_gateway.anomaly import AnomalyDetector, parse_window


@pytest.fixture(autouse=True)
def _clear_anomaly_cache():
    """Wipe the in-process anomaly cache before AND after each test.

    Tests run inside the same process; without a clear, the second test
    can pick up the first test's cached report.
    """

    anomaly.clear_cache()
    yield
    anomaly.clear_cache()


def _record(
    *,
    timestamp_iso: str,
    agent_id: str | None = "ag_a",
    tool_id: str = "web.fetch",
    cost_estimate_usd: float = 0.001,
    duration_ms: int = 50,
    cached: bool = False,
    error: str | None = None,
    tenant_id: str = "default",
) -> tuple[str, tuple]:
    """Build a raw INSERT-row tuple for direct DB seeding.

    We deliberately bypass :meth:`AuditLog.record` so tests can plant
    rows with arbitrary timestamps without monkey-patching ``utcnow``.
    """

    from plinth_gateway.audit import new_audit_id

    event_id = new_audit_id()
    sql = (
        "INSERT INTO audit_events ("
        "id, timestamp, tool_id, workspace_id, agent_id, tenant_id, "
        "arguments_hash, arguments_preview, result_hash, "
        "cached, duration_ms, cost_estimate_usd, error, prev_hash, event_hash"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    params = (
        event_id,
        timestamp_iso,
        tool_id,
        "ws_a",
        agent_id,
        tenant_id,
        "h" * 64,
        '{"url":"u"}',
        "r" * 64,
        1 if cached else 0,
        int(duration_ms),
        float(cost_estimate_usd),
        error,
        None,
        None,
    )
    return sql, params


async def _seed(db, sql_params_list: list[tuple[str, tuple]]) -> None:
    for sql, params in sql_params_list:
        await db.execute(sql, params)


def _iso(ts: datetime) -> str:
    return ts.isoformat()


# ---------------------------------------------------------------------------
# parse_window unit tests


def test_parse_window_supported_shapes() -> None:
    assert parse_window("1h") == timedelta(hours=1)
    assert parse_window("24h") == timedelta(hours=24)
    assert parse_window("7d") == timedelta(days=7)
    assert parse_window("30d") == timedelta(days=30)
    assert parse_window("30m") == timedelta(minutes=30)
    assert parse_window("60s") == timedelta(seconds=60)


def test_parse_window_invalid_raises() -> None:
    for bad in ["", "banana", "1y", "-1h", "0h", "1.5h"]:
        with pytest.raises(ValueError):
            parse_window(bad)


# ---------------------------------------------------------------------------
# Detector unit tests
#
# Each test pins ``now`` so the detector inspects deterministic rows.


@pytest.mark.asyncio
async def test_no_anomalies_in_flat_data(db) -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    rows = []
    # 30 minutes of identical, low-volume traffic.
    for i in range(30):
        ts = now - timedelta(minutes=30 - i)
        rows.append(_record(timestamp_iso=_iso(ts), cost_estimate_usd=0.001))
    await _seed(db, rows)

    detector = AnomalyDetector(db)
    report = await detector.detect(window="1h", use_cache=False, now=now)
    assert report.total_anomalies == 0
    assert report.anomalies == []


@pytest.mark.asyncio
async def test_cost_spike_critical(db) -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    rows = []
    # 60 minutes of $0 baseline, then a $5 spike right at "now".
    for i in range(60):
        ts = now - timedelta(minutes=60 - i)
        # Insert one row per baseline minute at near-zero cost.
        rows.append(_record(timestamp_iso=_iso(ts), cost_estimate_usd=0.0001))
    spike_minute = now - timedelta(seconds=30)
    rows.append(_record(timestamp_iso=_iso(spike_minute), cost_estimate_usd=5.00))
    await _seed(db, rows)

    detector = AnomalyDetector(db)
    report = await detector.detect(window="1h", use_cache=False, now=now)
    cost_anoms = [a for a in report.anomalies if a.type == "cost_spike"]
    assert cost_anoms, report.anomalies
    top = cost_anoms[0]
    assert top.severity == "critical"
    assert top.metric_value > 4.0
    assert top.z_score >= 3.0
    assert top.metric_name == "cost_usd_per_minute"


@pytest.mark.asyncio
async def test_rate_spike_critical(db) -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    rows = []
    # Baseline: 1 invocation per minute for 60 minutes.
    for i in range(60):
        ts = now - timedelta(minutes=60 - i, seconds=30)
        rows.append(_record(timestamp_iso=_iso(ts), cost_estimate_usd=0.0))
    # Spike: 100 invocations in the focus minute.
    spike_minute = now - timedelta(seconds=30)
    for _ in range(100):
        rows.append(_record(timestamp_iso=_iso(spike_minute), cost_estimate_usd=0.0))
    await _seed(db, rows)

    detector = AnomalyDetector(db)
    report = await detector.detect(window="1h", use_cache=False, now=now)
    rate_anoms = [a for a in report.anomalies if a.type == "rate_spike"]
    assert rate_anoms
    assert rate_anoms[0].severity == "critical"
    assert rate_anoms[0].metric_value >= 100.0


@pytest.mark.asyncio
async def test_error_spike_warning(db) -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    rows = []
    # 60 minutes of all-success.
    for i in range(60):
        ts = now - timedelta(minutes=60 - i, seconds=30)
        rows.append(_record(timestamp_iso=_iso(ts), tool_id="web.fetch"))
    # 10 minutes ago, 6 errors arrive in one minute (clean tool).
    spike_minute = now - timedelta(seconds=30)
    for _ in range(10):
        rows.append(_record(timestamp_iso=_iso(spike_minute), tool_id="web.fetch"))
    for _ in range(6):
        rows.append(
            _record(
                timestamp_iso=_iso(spike_minute),
                tool_id="web.fetch",
                error="boom",
            )
        )
    await _seed(db, rows)

    detector = AnomalyDetector(db)
    report = await detector.detect(window="1h", use_cache=False, now=now)
    err_anoms = [a for a in report.anomalies if a.type == "error_spike"]
    assert err_anoms, report.anomalies
    # 6 errors >= MIN_ERRORS, severity is at least warning.
    assert err_anoms[0].severity in ("warning", "critical")
    assert err_anoms[0].tool_id == "web.fetch"


@pytest.mark.asyncio
async def test_error_spike_below_minimum_no_anomaly(db) -> None:
    """Fewer than MIN_ERRORS should not fire — keeps low-traffic noise quiet."""
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    rows = []
    spike_minute = now - timedelta(seconds=30)
    for _ in range(4):  # below MIN_ERRORS=5
        rows.append(
            _record(
                timestamp_iso=_iso(spike_minute),
                tool_id="web.fetch",
                error="boom",
            )
        )
    await _seed(db, rows)
    detector = AnomalyDetector(db)
    report = await detector.detect(window="1h", use_cache=False, now=now)
    assert all(a.type != "error_spike" for a in report.anomalies)


@pytest.mark.asyncio
async def test_new_tool_info(db) -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    rows = []
    # Agent has called web.search regularly for the last 24h.
    for hours_ago in range(2, 24):
        ts = now - timedelta(hours=hours_ago)
        rows.append(_record(timestamp_iso=_iso(ts), tool_id="web.search"))
    # In the focus minute, the agent suddenly hits a brand-new tool.
    rows.append(
        _record(
            timestamp_iso=_iso(now - timedelta(seconds=30)),
            tool_id="email.send",
        )
    )
    await _seed(db, rows)

    detector = AnomalyDetector(db)
    report = await detector.detect(window="1h", use_cache=False, now=now)
    new_tool = [a for a in report.anomalies if a.type == "new_tool"]
    assert new_tool
    a = new_tool[0]
    assert a.severity == "info"
    assert a.tool_id == "email.send"
    assert a.agent_id == "ag_a"


@pytest.mark.asyncio
async def test_severity_filter(db) -> None:
    """Critical-only filter drops info/warning rows."""
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    rows = []
    # Fabricate a new-tool (info) anomaly only.
    for hours_ago in range(2, 24):
        rows.append(
            _record(
                timestamp_iso=_iso(now - timedelta(hours=hours_ago)),
                tool_id="web.search",
            )
        )
    rows.append(
        _record(
            timestamp_iso=_iso(now - timedelta(seconds=30)),
            tool_id="email.send",
        )
    )
    await _seed(db, rows)

    detector = AnomalyDetector(db)
    full = await detector.detect(window="1h", use_cache=False, now=now)
    assert any(a.severity == "info" for a in full.anomalies)
    critical_only = await detector.detect(
        window="1h", min_severity="critical", use_cache=False, now=now
    )
    assert all(a.severity == "critical" for a in critical_only.anomalies)


@pytest.mark.asyncio
async def test_type_filter(db) -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    rows = []
    # Two anomaly families in one fixture — cost spike + new tool.
    for i in range(60):
        ts = now - timedelta(minutes=60 - i, seconds=30)
        rows.append(_record(timestamp_iso=_iso(ts), cost_estimate_usd=0.0001))
    rows.append(
        _record(
            timestamp_iso=_iso(now - timedelta(seconds=30)),
            cost_estimate_usd=5.0,
            tool_id="email.send",
        )
    )
    await _seed(db, rows)

    detector = AnomalyDetector(db)
    only_cost = await detector.detect(
        window="1h", type_filter="cost_spike", use_cache=False, now=now
    )
    assert only_cost.anomalies
    assert all(a.type == "cost_spike" for a in only_cost.anomalies)


@pytest.mark.asyncio
async def test_cache_returns_identical_within_ttl(db) -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    # Single anomaly so the report is non-trivial.
    rows = []
    for hours_ago in range(2, 24):
        rows.append(
            _record(
                timestamp_iso=_iso(now - timedelta(hours=hours_ago)),
                tool_id="web.search",
            )
        )
    rows.append(
        _record(
            timestamp_iso=_iso(now - timedelta(seconds=30)),
            tool_id="email.send",
        )
    )
    await _seed(db, rows)

    detector = AnomalyDetector(db)
    first = await detector.detect(window="1h", use_cache=True, now=now)
    # Inserting more rows AFTER the first call should NOT change the
    # cached result, because the second call hits the in-process cache.
    extra = [
        _record(
            timestamp_iso=_iso(now - timedelta(seconds=30)),
            tool_id="another.tool",
        )
    ]
    await _seed(db, extra)
    second = await detector.detect(window="1h", use_cache=True, now=now)
    assert [a.id for a in second.anomalies] == [a.id for a in first.anomalies]


@pytest.mark.asyncio
async def test_cache_bypassed_when_disabled(db) -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    rows = []
    for hours_ago in range(2, 24):
        rows.append(
            _record(
                timestamp_iso=_iso(now - timedelta(hours=hours_ago)),
                tool_id="web.search",
            )
        )
    rows.append(
        _record(
            timestamp_iso=_iso(now - timedelta(seconds=30)),
            tool_id="email.send",
        )
    )
    await _seed(db, rows)
    detector = AnomalyDetector(db)
    first = await detector.detect(window="1h", use_cache=False, now=now)
    # New-tool detector also sees the second tool now if we add a row.
    rows = [
        _record(
            timestamp_iso=_iso(now - timedelta(seconds=29)),
            tool_id="vault.read",
        )
    ]
    await _seed(db, rows)
    second = await detector.detect(window="1h", use_cache=False, now=now)
    # Second call sees more anomalies (extra "new_tool" emit).
    assert second.total_anomalies >= first.total_anomalies


@pytest.mark.asyncio
async def test_unusual_pattern_emits_when_sequence_changes(db) -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    rows = []
    # 24-hour history: every minute for the past 23h calls "web.fetch"
    # alone. So the canonical sequence ["web.fetch"] is well-known.
    for hours_ago in range(2, 24):
        ts = now - timedelta(hours=hours_ago)
        rows.append(_record(timestamp_iso=_iso(ts), tool_id="web.fetch"))
    # Focus minute: the agent runs both web.fetch AND notes.add — an
    # unseen sequence.
    focus_ts = now - timedelta(seconds=30)
    rows.append(_record(timestamp_iso=_iso(focus_ts), tool_id="web.fetch"))
    rows.append(_record(timestamp_iso=_iso(focus_ts), tool_id="notes.add"))
    await _seed(db, rows)

    detector = AnomalyDetector(db)
    report = await detector.detect(window="1h", use_cache=False, now=now)
    pattern = [a for a in report.anomalies if a.type == "unusual_pattern"]
    assert pattern
    assert pattern[0].severity == "info"
    assert "notes.add" in pattern[0].raw_data["tools"]


@pytest.mark.asyncio
async def test_existing_pattern_not_flagged(db) -> None:
    """Repeating an already-seen sequence does not emit unusual_pattern."""
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    rows = []
    # Both "web.fetch" + "notes.add" co-occur regularly in the lookback.
    for hours_ago in range(2, 24):
        ts = now - timedelta(hours=hours_ago)
        rows.append(_record(timestamp_iso=_iso(ts), tool_id="web.fetch"))
        rows.append(_record(timestamp_iso=_iso(ts), tool_id="notes.add"))
    # Same combo again in the focus minute.
    focus_ts = now - timedelta(seconds=30)
    rows.append(_record(timestamp_iso=_iso(focus_ts), tool_id="web.fetch"))
    rows.append(_record(timestamp_iso=_iso(focus_ts), tool_id="notes.add"))
    await _seed(db, rows)

    detector = AnomalyDetector(db)
    report = await detector.detect(window="1h", use_cache=False, now=now)
    assert all(a.type != "unusual_pattern" for a in report.anomalies)


@pytest.mark.asyncio
async def test_agent_filter_scopes_focus(db) -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    rows = []
    # Agent A has a cost spike, agent B is quiet.
    for i in range(60):
        ts = now - timedelta(minutes=60 - i, seconds=30)
        rows.append(_record(timestamp_iso=_iso(ts), agent_id="ag_a", cost_estimate_usd=0.0001))
        rows.append(_record(timestamp_iso=_iso(ts), agent_id="ag_b", cost_estimate_usd=0.0001))
    spike_minute = now - timedelta(seconds=30)
    rows.append(
        _record(
            timestamp_iso=_iso(spike_minute),
            agent_id="ag_a",
            cost_estimate_usd=5.0,
        )
    )
    await _seed(db, rows)

    detector = AnomalyDetector(db)
    report_for_b = await detector.detect(
        window="1h", agent_id="ag_b", use_cache=False, now=now
    )
    assert all(
        a.type != "cost_spike" or a.agent_id == "ag_b"
        for a in report_for_b.anomalies
    )


# ---------------------------------------------------------------------------
# HTTP endpoint tests


@pytest.mark.asyncio
async def test_endpoint_empty_returns_zero_anomalies(client) -> None:
    r = await client.get("/v1/audit/anomalies?window=1h")
    assert r.status_code == 200
    body = r.json()
    assert body["total_anomalies"] == 0
    assert body["anomalies"] == []
    assert body["window"] == "1h"


@pytest.mark.asyncio
async def test_endpoint_invalid_window(client) -> None:
    r = await client.get("/v1/audit/anomalies?window=banana")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_endpoint_invalid_severity(client) -> None:
    r = await client.get("/v1/audit/anomalies?window=1h&min_severity=meh")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_invalid_type(client) -> None:
    r = await client.get("/v1/audit/anomalies?window=1h&type=time_warp")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_returns_anomalies(app_and_client) -> None:
    """End-to-end: seed real audit rows, hit the endpoint, see anomalies."""
    app, client = app_and_client
    db = app.state.db
    # Use a stable "now" anchored a couple of seconds in the past so the
    # spike row is comfortably inside the floor-of-the-current-minute and
    # we don't dance with second-of-the-minute edge cases.
    now = datetime.now(timezone.utc) - timedelta(seconds=5)
    rows = []
    # Two minutes ago through one hour and two minutes ago — leave the
    # most recent minute clear of baseline rows so the focus window is
    # unambiguous.
    for minutes_ago in range(2, 62):
        ts = now - timedelta(minutes=minutes_ago)
        rows.append(_record(timestamp_iso=_iso(ts), cost_estimate_usd=0.0001))
    # Spike: 10 USD in the focus minute (one minute ago).
    rows.append(
        _record(
            timestamp_iso=_iso(now - timedelta(seconds=30)),
            cost_estimate_usd=10.0,
        )
    )
    for sql, params in rows:
        await db.execute(sql, params)
    # Force the cache to miss by clearing it after lifespan startup.
    anomaly.clear_cache()

    r = await client.get("/v1/audit/anomalies?window=1h")
    assert r.status_code == 200
    body = r.json()
    assert body["total_anomalies"] >= 1
    types = {a["type"] for a in body["anomalies"]}
    assert "cost_spike" in types
