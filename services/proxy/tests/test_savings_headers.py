# SPDX-License-Identifier: Apache-2.0
"""The ``X-Plynf-*`` per-call savings headers should ride on every chat /
native-dialect front door, so any HTTP client sees the token reduction inline
without polling ``/v1/savings/summary``."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings

_HEADER_KEYS = (
    "x-plynf-tool-calls",
    "x-plynf-raw-tokens",
    "x-plynf-shaped-tokens",
    "x-plynf-saved-tokens",
    "x-plynf-savings-pct",
)


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def _assert_header_invariants(headers, *, expect_tool_call: bool):
    # All five headers must be present (header names are case-insensitive).
    for key in _HEADER_KEYS:
        assert key in headers, f"missing {key}"

    tool_calls = int(headers["x-plynf-tool-calls"])
    raw = int(headers["x-plynf-raw-tokens"])
    shaped = int(headers["x-plynf-shaped-tokens"])
    saved = int(headers["x-plynf-saved-tokens"])
    pct = float(headers["x-plynf-savings-pct"])

    # saved == raw - shaped, and savings can't be negative or exceed 100%.
    assert saved == raw - shaped
    assert 0.0 <= pct <= 1.0
    if raw:
        assert pct == pytest.approx(saved / raw, abs=1e-4)

    if expect_tool_call:
        assert tool_calls >= 1
        assert raw >= shaped  # shaping never grows the payload
    else:
        # A plain completion intercepts no tools → all-zero, honest headers.
        assert tool_calls == 0
        assert raw == shaped == saved == 0
        assert pct == 0.0


# ---------------------------------------------------------------------------
# Request bodies that exercise the get_order tool round-trip per dialect
# ---------------------------------------------------------------------------

_OPENAI_TOOL_BODY = {
    "messages": [{"role": "user", "content": "where is my order?"}],
    "tools": [
        {
            "type": "function",
            "function": {"name": "get_order", "parameters": {"type": "object", "properties": {}}},
        }
    ],
}

_ANTHROPIC_TOOL_BODY = {
    "model": "claude-3-5-sonnet",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "order 12345"}],
    "tools": [{"name": "get_order", "description": "x", "input_schema": {"type": "object"}}],
}

_GEMINI_TOOL_BODY = {
    "contents": [{"role": "user", "parts": [{"text": "where is my order?"}]}],
    "tools": [
        {"functionDeclarations": [{"name": "get_order", "parameters": {"type": "object"}}]}
    ],
}

_BEDROCK_TOOL_BODY = {
    "messages": [{"role": "user", "content": [{"text": "order 12345"}]}],
    "toolConfig": {
        "tools": [
            {
                "toolSpec": {
                    "name": "get_order",
                    "description": "x",
                    "inputSchema": {"json": {"type": "object"}},
                }
            }
        ]
    },
}

_COHERE_TOOL_BODY = {
    "model": "command-r-plus",
    "messages": [{"role": "user", "content": "where is my order?"}],
    "tools": [
        {"type": "function", "function": {"name": "get_order", "parameters": {"type": "object"}}}
    ],
}

_RESPONSES_TOOL_BODY = {
    "model": "gpt-4o",
    "input": "where is my order?",
    "tools": [{"type": "function", "name": "get_order", "parameters": {"type": "object"}}],
}


def test_openai_chat_emits_savings_headers(demo_client):
    r = demo_client.post("/v1/chat/completions", json=_OPENAI_TOOL_BODY)
    assert r.status_code == 200
    _assert_header_invariants(r.headers, expect_tool_call=True)


def test_azure_chat_emits_savings_headers(demo_client):
    r = demo_client.post("/openai/deployments/gpt-4o/chat/completions", json=_OPENAI_TOOL_BODY)
    assert r.status_code == 200
    _assert_header_invariants(r.headers, expect_tool_call=True)


def test_anthropic_emits_savings_headers(demo_client):
    r = demo_client.post("/v1/messages", json=_ANTHROPIC_TOOL_BODY)
    assert r.status_code == 200
    _assert_header_invariants(r.headers, expect_tool_call=True)


def test_gemini_emits_savings_headers(demo_client):
    r = demo_client.post(
        "/v1beta/models/gemini-1.5-pro:generateContent", json=_GEMINI_TOOL_BODY
    )
    assert r.status_code == 200
    _assert_header_invariants(r.headers, expect_tool_call=True)


def test_bedrock_emits_savings_headers(demo_client):
    r = demo_client.post(
        "/model/anthropic.claude-3-5-sonnet/converse", json=_BEDROCK_TOOL_BODY
    )
    assert r.status_code == 200
    _assert_header_invariants(r.headers, expect_tool_call=True)


def test_cohere_emits_savings_headers(demo_client):
    r = demo_client.post("/v2/chat", json=_COHERE_TOOL_BODY)
    assert r.status_code == 200
    _assert_header_invariants(r.headers, expect_tool_call=True)


def test_responses_emits_savings_headers(demo_client):
    r = demo_client.post("/v1/responses", json=_RESPONSES_TOOL_BODY)
    assert r.status_code == 200
    _assert_header_invariants(r.headers, expect_tool_call=True)


def test_plain_completion_has_zero_savings_headers(demo_client):
    # No tools → no interception → headers present but all zero.
    r = demo_client.post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert r.status_code == 200
    _assert_header_invariants(r.headers, expect_tool_call=False)


def test_streaming_response_carries_savings_headers(demo_client):
    body = dict(_OPENAI_TOOL_BODY, stream=True)
    r = demo_client.post("/v1/chat/completions", json=body)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    # Streaming responses still advertise the savings on the response headers,
    # and keep the SSE proxy hints.
    assert r.headers["cache-control"] == "no-cache"
    _assert_header_invariants(r.headers, expect_tool_call=True)
