# SPDX-License-Identifier: Apache-2.0
"""Tests for the context-budget manager."""

from __future__ import annotations

import json

from plinth_proxy.context_budget import enforce_budget
from plinth_proxy.tokens import count_messages_tokens


def _tool_msg(call_id: str, payload: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "get_order",
        "content": json.dumps(payload),
    }


def _big_payload(approx_tokens: int) -> dict:
    """Build a payload that tokenises to roughly the requested size.

    We use a sequence of *unique* short strings instead of a long run of one
    character — BPE compresses ``"xxx…"`` into a handful of tokens, which
    would make this helper lie about its size.
    """
    items = [f"field_{i}: data_value_{i}_payload" for i in range(approx_tokens)]
    return {"junk": items}


def test_budget_no_op_when_under_budget():
    messages = [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "Hello"},
    ]
    out, dropped = enforce_budget(messages, max_input_tokens=10_000)
    assert out == messages
    assert dropped == 0


def test_budget_rotates_oldest_tool_messages_first():
    messages = [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "process all orders"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        _tool_msg("c1", _big_payload(500)),
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c2"}]},
        _tool_msg("c2", _big_payload(500)),
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c3"}]},
        _tool_msg("c3", _big_payload(500)),
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c4"}]},
        _tool_msg("c4", _big_payload(500)),
        {"role": "user", "content": "anything else?"},
    ]
    out, dropped = enforce_budget(
        messages, max_input_tokens=1500, keep_recent_tool_messages=2
    )
    # We rotated the oldest tool messages first (c1, c2 — c3 and c4 protected).
    summarised = [
        m for m in out
        if m.get("role") == "tool"
        and "_plynf_summarised" in (m.get("content") or "")
    ]
    intact = [
        m for m in out
        if m.get("role") == "tool"
        and "_plynf_summarised" not in (m.get("content") or "")
    ]
    summarised_ids = {m["tool_call_id"] for m in summarised}
    intact_ids = {m["tool_call_id"] for m in intact}
    # c3 + c4 are the most recent — must remain intact.
    assert "c3" in intact_ids
    assert "c4" in intact_ids
    # c1 was rotated first.
    assert "c1" in summarised_ids
    assert dropped > 0


def test_budget_preserves_message_count_and_tool_call_id_mapping():
    """Summarised messages MUST still be ``role: tool`` with the original ID."""
    messages = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "tool_calls": [{"id": "abc"}]},
        _tool_msg("abc", _big_payload(800)),
        {"role": "assistant", "tool_calls": [{"id": "def"}]},
        _tool_msg("def", _big_payload(800)),
        {"role": "user", "content": "next"},
    ]
    out, dropped = enforce_budget(
        messages, max_input_tokens=1000, keep_recent_tool_messages=1
    )
    assert len(out) == len(messages)
    # ID linkage is preserved.
    tools = [m for m in out if m.get("role") == "tool"]
    assert {m["tool_call_id"] for m in tools} == {"abc", "def"}


def test_budget_does_not_rotate_if_only_recent_tools_present():
    """If all tool messages are within the 'keep recent' window, no rotation."""
    messages = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "tool_calls": [{"id": "a"}]},
        _tool_msg("a", _big_payload(2000)),  # huge, but only 1 tool message
    ]
    out, dropped = enforce_budget(
        messages, max_input_tokens=100, keep_recent_tool_messages=3
    )
    # Nothing eligible to rotate; the manager refuses to silently drop it.
    assert dropped == 0
    assert out == messages


def test_budget_reports_no_negative_drop():
    """If rotation somehow doesn't free space, dropped is non-negative."""
    messages = [
        {"role": "system", "content": "tiny"},
        {"role": "user", "content": "tiny"},
        {"role": "assistant", "tool_calls": [{"id": "x"}]},
        _tool_msg("x", {"_short": True}),  # smaller than the summary placeholder
        {"role": "user", "content": "next"},
    ]
    out, dropped = enforce_budget(
        messages, max_input_tokens=5, keep_recent_tool_messages=0
    )
    assert dropped >= 0


def test_budget_reduces_actual_token_count():
    messages = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "tool_calls": [{"id": "a"}]},
        _tool_msg("a", _big_payload(1500)),
        {"role": "assistant", "tool_calls": [{"id": "b"}]},
        _tool_msg("b", _big_payload(50)),
    ]
    before = count_messages_tokens(messages)
    out, dropped = enforce_budget(
        messages, max_input_tokens=200, keep_recent_tool_messages=1
    )
    after = count_messages_tokens(out)
    assert after < before
    assert dropped == before - after or dropped > 0
