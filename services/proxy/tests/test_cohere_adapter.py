# SPDX-License-Identifier: Apache-2.0
"""Tests for the Cohere v2 ``/v2/chat`` front-door adapter + endpoint."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.cohere_adapter import (
    cohere_chat_request_to_openai,
    openai_response_to_cohere_chat,
)
from plinth_proxy.settings import ProxySettings

# ---------------------------------------------------------------------------
# Request translation: Cohere v2 → OpenAI
# ---------------------------------------------------------------------------


def test_request_basic_user_message():
    out = cohere_chat_request_to_openai(
        {"model": "command-r-plus", "messages": [{"role": "user", "content": "hello"}]}
    )
    assert out["model"] == "command-r-plus"
    assert out["messages"] == [{"role": "user", "content": "hello"}]


def test_request_model_override_wins():
    out = cohere_chat_request_to_openai(
        {"model": "command-r", "messages": []}, model="gpt-4o-mini"
    )
    assert out["model"] == "gpt-4o-mini"


def test_request_system_and_user():
    out = cohere_chat_request_to_openai(
        {
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ]
        }
    )
    assert out["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]


def test_request_tool_call_roundtrip():
    body = {
        "messages": [
            {"role": "user", "content": "where is order 12345?"},
            {
                "role": "assistant",
                "content": None,
                "tool_plan": "I'll look up the order",  # dropped on the way in
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_order",
                            "arguments": '{"order_id": "12345"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": [
                    {
                        "type": "document",
                        "document": {"data": {"order_id": "12345", "status": "shipped"}},
                    }
                ],
            },
        ]
    }
    out = cohere_chat_request_to_openai(body)
    msgs = out["messages"]

    assert msgs[0] == {"role": "user", "content": "where is order 12345?"}

    assistant = msgs[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] is None
    assert "tool_plan" not in assistant
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert assistant["tool_calls"][0]["function"]["name"] == "get_order"

    tool_msg = msgs[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_1"
    assert "shipped" in tool_msg["content"]  # document flattened to a JSON string


def test_request_flattens_text_content_blocks():
    out = cohere_chat_request_to_openai(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hello "},
                        {"type": "text", "text": "world"},
                    ],
                }
            ]
        }
    )
    assert out["messages"][0] == {"role": "user", "content": "hello world"}


def test_request_tools_passthrough():
    out = cohere_chat_request_to_openai(
        {
            "messages": [],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_order",
                        "description": "Look up an order",
                        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
                    },
                }
            ],
        }
    )
    assert out["tools"][0]["type"] == "function"
    assert out["tools"][0]["function"]["name"] == "get_order"
    assert out["tools"][0]["function"]["parameters"]["properties"]["id"]["type"] == "string"


@pytest.mark.parametrize(
    "cohere_choice,expected",
    [("REQUIRED", "required"), ("NONE", "none"), ("AUTO", "auto")],
)
def test_request_tool_choice_mapping(cohere_choice, expected):
    out = cohere_chat_request_to_openai(
        {"messages": [], "tool_choice": cohere_choice}
    )
    assert out["tool_choice"] == expected


def test_request_inference_knobs():
    out = cohere_chat_request_to_openai(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.4,
            "max_tokens": 256,
            "p": 0.8,  # Cohere spells top-p as "p"
            "stop_sequences": ["END"],
        }
    )
    assert out["temperature"] == 0.4
    assert out["max_tokens"] == 256
    assert out["top_p"] == 0.8
    assert out["stop"] == ["END"]


# ---------------------------------------------------------------------------
# Response translation: OpenAI → Cohere v2
# ---------------------------------------------------------------------------


def test_response_text_message():
    resp = {
        "id": "chatcmpl-x",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Hello there"},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }
    out = openai_response_to_cohere_chat(resp)
    assert out["id"] == "chatcmpl-x"
    assert out["finish_reason"] == "COMPLETE"
    assert out["message"]["content"] == [{"type": "text", "text": "Hello there"}]
    assert out["usage"]["tokens"] == {"input_tokens": 10, "output_tokens": 3}
    assert out["usage"]["billed_units"] == {"input_tokens": 10, "output_tokens": 3}


def test_response_tool_calls():
    resp = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_9",
                            "type": "function",
                            "function": {
                                "name": "get_order",
                                "arguments": '{"order_id": "1"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {},
    }
    out = openai_response_to_cohere_chat(resp)
    assert out["finish_reason"] == "TOOL_CALL"
    # No assistant text → no content key.
    assert "content" not in out["message"]
    tc = out["message"]["tool_calls"][0]
    assert tc["id"] == "call_9"
    assert tc["function"]["name"] == "get_order"
    assert isinstance(tc["function"]["arguments"], str)


def test_response_coerces_dict_arguments_to_string():
    resp = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "x", "arguments": {"a": 1}},
                        }
                    ]
                },
            }
        ]
    }
    out = openai_response_to_cohere_chat(resp)
    args = out["message"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args) == {"a": 1}


@pytest.mark.parametrize(
    "openai_reason,cohere_reason",
    [
        ("stop", "COMPLETE"),
        ("tool_calls", "TOOL_CALL"),
        ("length", "MAX_TOKENS"),
        ("content_filter", "ERROR_TOXIC"),
        ("something_new", "COMPLETE"),  # unknown → safe default
    ],
)
def test_response_finish_reason_map(openai_reason, cohere_reason):
    resp = {"choices": [{"finish_reason": openai_reason, "message": {"content": "x"}}]}
    assert openai_response_to_cohere_chat(resp)["finish_reason"] == cohere_reason


# ---------------------------------------------------------------------------
# End-to-end through the proxy (demo mode)
# ---------------------------------------------------------------------------


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def test_cohere_endpoint_tool_roundtrip_and_savings(demo_client):
    body = {
        "model": "command-r-plus",
        "messages": [{"role": "user", "content": "where is my order?"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_order",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }
    r = demo_client.post("/v2/chat", json=body)
    assert r.status_code == 200
    data = r.json()

    assert data["message"]["role"] == "assistant"
    # The mock answers with a text summary after the tool round-trip.
    assert any(b.get("type") == "text" for b in data["message"].get("content", []))
    assert data["finish_reason"] == "COMPLETE"
    assert "tokens" in data["usage"]

    # The interception was measured.
    summary = demo_client.get("/v1/savings/summary").json()
    assert summary["total_calls"] >= 1
