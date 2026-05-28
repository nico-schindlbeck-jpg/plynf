# SPDX-License-Identifier: Apache-2.0
"""Tests for in-round cross-call merging.

When the LLM emits multiple identical tool_calls in one round (same name,
same arguments), Plynf executes the tool once and replays the shaped
result across all tool_call_ids. The dashboard reflects this as cache
hits, so we get both the latency win (fewer external calls) and a
quantified savings line.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from plinth_proxy import api as proxy_api
from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings


def _build_app():
    return create_app(ProxySettings(demo_mode=True))


def _two_identical_tool_calls(model: str = "gpt-4o") -> dict:
    """An LLM response that asks for the same tool twice."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_a",
                            "type": "function",
                            "function": {
                                "name": "get_order",
                                "arguments": '{"order_id":"12345"}',
                            },
                        },
                        {
                            "id": "call_b",
                            "type": "function",
                            "function": {
                                "name": "get_order",
                                "arguments": '{"order_id":"12345"}',
                            },
                        },
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _final_text(content: str = "All set.") -> dict:
    return {
        "id": "chatcmpl-final",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@pytest.fixture
def app_with_dup_tool_call(monkeypatch):
    """Inject a scripted upstream: first call returns 2 dup tool_calls; next returns plain text."""
    sequence = [_two_identical_tool_calls(), _final_text()]
    call_count = {"n": 0}

    async def fake_upstream(*_args, **_kwargs):
        i = call_count["n"]
        call_count["n"] += 1
        return sequence[min(i, len(sequence) - 1)]

    monkeypatch.setattr(proxy_api, "_call_upstream", fake_upstream)
    return _build_app()


def test_duplicate_tool_calls_execute_tool_once(app_with_dup_tool_call):
    client = TestClient(app_with_dup_tool_call)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "what is order 12345"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "get_order"},
                }
            ],
        },
    )
    assert r.status_code == 200

    # Two events should be logged: one real execution and one "merged"
    # (cache_hit=True) event for the duplicate.
    state = app_with_dup_tool_call.state.plinth
    rounds = [e for e in state.events if e.tool == "get_order"]
    assert len(rounds) == 2, f"expected 2 events, got {len(rounds)}: {rounds}"
    real = [e for e in rounds if not e.cache_hit]
    merged = [e for e in rounds if e.cache_hit]
    assert len(real) == 1
    assert len(merged) == 1


def test_savings_summary_reports_cache_hit_rate_for_merged_calls(app_with_dup_tool_call):
    client = TestClient(app_with_dup_tool_call)
    client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "get_order"}}],
        },
    )
    summary = client.get("/v1/savings/summary").json()
    assert summary["total_calls"] == 2
    # One merged duplicate, so cache_hit_rate is exactly 0.5.
    assert summary["cache_hit_rate"] == 0.5


def test_non_duplicate_tool_calls_run_normally(monkeypatch):
    """Two different tool_calls should NOT be merged."""

    def two_different():
        resp = _two_identical_tool_calls()
        resp["choices"][0]["message"]["tool_calls"][1]["function"]["arguments"] = (
            '{"order_id":"99999"}'
        )
        return resp

    sequence = [two_different(), _final_text()]
    call_count = {"n": 0}

    async def fake_upstream(*_args, **_kwargs):
        i = call_count["n"]
        call_count["n"] += 1
        return sequence[min(i, len(sequence) - 1)]

    monkeypatch.setattr(proxy_api, "_call_upstream", fake_upstream)
    app = _build_app()
    client = TestClient(app)
    client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "get_order"}}],
        },
    )
    state = app.state.plinth
    events = [e for e in state.events if e.tool == "get_order"]
    assert len(events) == 2
    # Neither is a cache hit because they had different args.
    assert all(not e.cache_hit for e in events)
