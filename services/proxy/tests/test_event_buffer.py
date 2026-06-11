# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""The bounded EventBuffer must behave like the list it replaced AND cap memory."""

from __future__ import annotations

from plinth_proxy.savings import EventBuffer, aggregate, make_event


def _ev(saved: int = 10):
    return make_event(
        tenant_id="t",
        agent_id=None,
        connector="orders",
        tool="get_order",
        model="gpt-4o",
        raw_response_tokens=saved * 2,
        shaped_response_tokens=saved,
        cache_hit=False,
        request_args={},
    )


def test_len_is_total_appended_and_slice_returns_request_window():
    buf = EventBuffer(maxlen=10)
    for _ in range(3):
        buf.append(_ev())
    before = len(buf)              # snapshot position == 3
    assert before == 3
    buf.append(_ev())
    buf.append(_ev())
    window = buf[before:]         # exactly the 2 appended after the snapshot
    assert len(window) == 2


def test_negative_index_and_iteration():
    buf = EventBuffer(maxlen=10)
    e_first = _ev(5)
    buf.append(e_first)
    buf.append(_ev(7))
    assert buf[-1].shaped_response_tokens == 7
    assert sum(e.raw_response_tokens for e in buf) == 5 * 2 + 7 * 2


def test_bounds_memory_but_keeps_absolute_positions():
    buf = EventBuffer(maxlen=100)
    for _ in range(1000):
        buf.append(_ev())
    # Only 100 retained physically …
    assert len(list(iter(buf))) == 100
    # … but len() is the true total, so a fresh snapshot+slice still works.
    before = len(buf)
    assert before == 1000
    buf.append(_ev())
    assert len(buf[before:]) == 1
    # A snapshot older than the retention window clamps to retained events
    # (never raises, never returns bogus rows).
    assert len(buf[0:]) == 100


def test_aggregate_consumes_buffer_like_a_list():
    buf = EventBuffer(maxlen=10)
    for _ in range(4):
        buf.append(_ev(10))
    summary = aggregate(buf)
    assert summary["total_calls"] == 4
    assert summary["total_saved_tokens"] == 40


def test_clear_resets_total():
    buf = EventBuffer(maxlen=10)
    buf.append(_ev())
    buf.clear()
    assert len(buf) == 0
    assert list(iter(buf)) == []
