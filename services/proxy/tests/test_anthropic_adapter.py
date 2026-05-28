# SPDX-License-Identifier: Apache-2.0
"""Tests for the Anthropic /v1/messages translation layer + endpoint."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.anthropic_adapter import (
    anthropic_request_to_openai,
    openai_response_to_anthropic,
)
from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings

# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------


def test_simple_user_message_translates():
    anth = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    openai = anthropic_request_to_openai(anth)
    assert openai["model"] == "claude-3-5-sonnet"
    assert openai["messages"] == [{"role": "user", "content": "Hello"}]
    assert openai["max_tokens"] == 1024


def test_system_field_becomes_system_message():
    anth = {
        "model": "claude-3-5-sonnet",
        "system": "You are a support agent.",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    openai = anthropic_request_to_openai(anth)
    assert openai["messages"][0] == {"role": "system", "content": "You are a support agent."}
    assert openai["messages"][1] == {"role": "user", "content": "Hi"}


def test_system_as_content_blocks():
    anth = {
        "model": "claude-3-5-sonnet",
        "system": [
            {"type": "text", "text": "Line one."},
            {"type": "text", "text": "Line two."},
        ],
        "messages": [{"role": "user", "content": "Hi"}],
    }
    openai = anthropic_request_to_openai(anth)
    assert openai["messages"][0]["role"] == "system"
    assert "Line one." in openai["messages"][0]["content"]
    assert "Line two." in openai["messages"][0]["content"]


def test_tool_use_in_assistant_becomes_openai_tool_calls():
    anth = {
        "model": "claude-3-5-sonnet",
        "messages": [
            {"role": "user", "content": "Order 12345?"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "get_order",
                        "input": {"order_id": "12345"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": "in_transit",
                    }
                ],
            },
        ],
    }
    openai = anthropic_request_to_openai(anth)
    # Expected sequence: user prompt, then a tool message (the result), then
    # the assistant tool_calls? No — Anthropic puts the tool_use *before* the
    # tool_result, and the OpenAI conversation reads similarly: user, then
    # assistant w/ tool_calls, then tool result.
    roles = [m["role"] for m in openai["messages"]]
    assert "tool" in roles
    assert any(
        m.get("role") == "assistant" and m.get("tool_calls") for m in openai["messages"]
    )
    # Tool call arguments are JSON-encoded.
    asst = next(m for m in openai["messages"] if m.get("tool_calls"))
    tc = asst["tool_calls"][0]
    assert tc["function"]["name"] == "get_order"
    assert json.loads(tc["function"]["arguments"]) == {"order_id": "12345"}
    # Tool result preserves the original tool_use_id.
    tool_msg = next(m for m in openai["messages"] if m.get("role") == "tool")
    assert tool_msg["tool_call_id"] == "toolu_abc"
    assert tool_msg["content"] == "in_transit"


def test_tool_definitions_translate():
    anth = {
        "model": "claude-3-5-sonnet",
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [
            {
                "name": "get_order",
                "description": "Fetch order by id",
                "input_schema": {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
            }
        ],
    }
    openai = anthropic_request_to_openai(anth)
    assert openai["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_order",
                "description": "Fetch order by id",
                "parameters": {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
            },
        }
    ]


# ---------------------------------------------------------------------------
# Response translation
# ---------------------------------------------------------------------------


def test_plain_text_response_translates():
    openai_resp = {
        "id": "chatcmpl-1",
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello there."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    }
    anth = openai_response_to_anthropic(openai_resp)
    assert anth["type"] == "message"
    assert anth["role"] == "assistant"
    assert anth["content"] == [{"type": "text", "text": "Hello there."}]
    assert anth["stop_reason"] == "end_turn"
    assert anth["usage"]["input_tokens"] == 10
    assert anth["usage"]["output_tokens"] == 4


def test_tool_call_response_translates_to_tool_use_block():
    openai_resp = {
        "id": "chatcmpl-2",
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_xyz",
                            "type": "function",
                            "function": {
                                "name": "get_order",
                                "arguments": '{"order_id":"12345"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    anth = openai_response_to_anthropic(openai_resp)
    assert anth["stop_reason"] == "tool_use"
    assert any(b["type"] == "tool_use" for b in anth["content"])
    tu = next(b for b in anth["content"] if b["type"] == "tool_use")
    assert tu["name"] == "get_order"
    assert tu["input"] == {"order_id": "12345"}
    assert tu["id"] == "call_xyz"


# ---------------------------------------------------------------------------
# End-to-end via the FastAPI app
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def test_v1_messages_endpoint_returns_anthropic_shape(client):
    anth_request = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "system": "You are a customer support agent.",
        "messages": [
            {"role": "user", "content": "What is the status of order 12345?"}
        ],
        "tools": [
            {
                "name": "get_order",
                "description": "Fetch order by id",
                "input_schema": {"type": "object"},
            }
        ],
    }
    r = client.post("/v1/messages", json=anth_request)
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert isinstance(body["content"], list)
    text = "".join(
        b.get("text", "") for b in body["content"] if b.get("type") == "text"
    )
    assert "12345" in text


def test_v1_messages_endpoint_rejects_streaming_in_mvp(client):
    r = client.post(
        "/v1/messages",
        json={
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert r.status_code == 501


def test_v1_messages_endpoint_emits_savings(client):
    client.post(
        "/v1/messages",
        json={
            "model": "claude-3-5-sonnet",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "order 12345"}],
            "tools": [
                {
                    "name": "get_order",
                    "description": "x",
                    "input_schema": {"type": "object"},
                }
            ],
        },
    )
    summary = client.get("/v1/savings/summary").json()
    assert summary["total_calls"] >= 1
    assert summary["total_saved_tokens"] > 0
