# SPDX-License-Identifier: Apache-2.0
"""Tests for the OpenAI *Responses* API ``/v1/responses`` front-door adapter."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.responses_adapter import (
    openai_response_to_responses,
    responses_request_to_openai,
)
from plinth_proxy.settings import ProxySettings

# ---------------------------------------------------------------------------
# Request translation: Responses → OpenAI chat
# ---------------------------------------------------------------------------


def test_request_string_input_becomes_user_message():
    out = responses_request_to_openai({"model": "gpt-4o", "input": "hello"})
    assert out["model"] == "gpt-4o"
    assert out["messages"] == [{"role": "user", "content": "hello"}]


def test_request_model_override_wins():
    out = responses_request_to_openai({"model": "gpt-4o", "input": "hi"}, model="gpt-4o-mini")
    assert out["model"] == "gpt-4o-mini"


def test_request_instructions_become_system_message():
    out = responses_request_to_openai({"instructions": "be brief", "input": "hi"})
    assert out["messages"][0] == {"role": "system", "content": "be brief"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_request_message_list_and_developer_role():
    out = responses_request_to_openai(
        {
            "input": [
                {"role": "developer", "content": "be terse"},
                {"role": "user", "content": "hi"},
            ]
        }
    )
    # The Responses-era "developer" role maps back to chat's "system".
    assert out["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]


def test_request_flattens_input_text_parts():
    out = responses_request_to_openai(
        {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "hello "},
                        {"type": "input_text", "text": "world"},
                    ],
                }
            ]
        }
    )
    assert out["messages"][0] == {"role": "user", "content": "hello world"}


def test_request_function_call_and_output_roundtrip():
    body = {
        "input": [
            {"role": "user", "content": "where is order 12345?"},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "get_order",
                "arguments": '{"order_id": "12345"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": '{"status": "shipped"}',
            },
        ]
    }
    msgs = responses_request_to_openai(body)["messages"]

    assert msgs[0] == {"role": "user", "content": "where is order 12345?"}

    assistant = msgs[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] is None
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert assistant["tool_calls"][0]["function"]["name"] == "get_order"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {
        "order_id": "12345"
    }

    tool_msg = msgs[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_1"
    assert "shipped" in tool_msg["content"]


def test_request_coalesces_consecutive_function_calls():
    """Parallel tool calls must ride on a single assistant message."""
    body = {
        "input": [
            {"type": "function_call", "call_id": "a", "name": "f", "arguments": "{}"},
            {"type": "function_call", "call_id": "b", "name": "g", "arguments": "{}"},
        ]
    }
    msgs = responses_request_to_openai(body)["messages"]
    assert len(msgs) == 1
    assert [tc["id"] for tc in msgs[0]["tool_calls"]] == ["a", "b"]


def test_request_coerces_dict_arguments_to_string():
    body = {
        "input": [
            {"type": "function_call", "call_id": "c", "name": "f", "arguments": {"a": 1}}
        ]
    }
    msgs = responses_request_to_openai(body)["messages"]
    args = msgs[0]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args) == {"a": 1}


def test_request_flat_function_tools_to_nested():
    out = responses_request_to_openai(
        {
            "input": [],
            "tools": [
                {
                    "type": "function",
                    "name": "get_order",
                    "description": "Look up an order",
                    "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
                }
            ],
        }
    )
    assert out["tools"][0]["type"] == "function"
    assert out["tools"][0]["function"]["name"] == "get_order"
    assert out["tools"][0]["function"]["parameters"]["properties"]["id"]["type"] == "string"


def test_request_skips_hosted_tools():
    out = responses_request_to_openai(
        {
            "input": [],
            "tools": [
                {"type": "web_search"},
                {"type": "function", "name": "get_order", "parameters": {"type": "object"}},
            ],
        }
    )
    # Only the function tool survives; the hosted web_search is dropped.
    assert len(out["tools"]) == 1
    assert out["tools"][0]["function"]["name"] == "get_order"


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("auto", "auto"),
        ("none", "none"),
        ("required", "required"),
        (
            {"type": "function", "name": "get_order"},
            {"type": "function", "function": {"name": "get_order"}},
        ),
    ],
)
def test_request_tool_choice_mapping(choice, expected):
    out = responses_request_to_openai({"input": [], "tool_choice": choice})
    assert out["tool_choice"] == expected


def test_request_inference_knobs():
    out = responses_request_to_openai(
        {
            "input": "hi",
            "max_output_tokens": 128,
            "temperature": 0.2,
            "top_p": 0.9,
        }
    )
    assert out["max_tokens"] == 128
    assert out["temperature"] == 0.2
    assert out["top_p"] == 0.9


# ---------------------------------------------------------------------------
# Response translation: OpenAI chat → Responses
# ---------------------------------------------------------------------------


def test_response_text_message():
    resp = {
        "id": "chatcmpl-x",
        "model": "gpt-4o",
        "created": 1700000000,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Hello there"},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }
    out = openai_response_to_responses(resp)
    assert out["id"] == "chatcmpl-x"
    assert out["object"] == "response"
    assert out["created_at"] == 1700000000
    assert out["status"] == "completed"
    assert out["output_text"] == "Hello there"

    item = out["output"][0]
    assert item["type"] == "message"
    assert item["role"] == "assistant"
    assert item["content"] == [
        {"type": "output_text", "text": "Hello there", "annotations": []}
    ]

    assert out["usage"] == {
        "input_tokens": 10,
        "output_tokens": 3,
        "total_tokens": 13,
    }


def test_response_tool_call_to_function_call_item():
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
    out = openai_response_to_responses(resp)
    # No assistant text → no message item, just the function_call.
    assert out["output_text"] == ""
    fc = out["output"][0]
    assert fc["type"] == "function_call"
    assert fc["call_id"] == "call_9"
    assert fc["name"] == "get_order"
    assert json.loads(fc["arguments"]) == {"order_id": "1"}
    assert fc["status"] == "completed"


def test_response_text_precedes_function_call():
    resp = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "let me check",
                    "tool_calls": [
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                    ],
                },
            }
        ],
    }
    out = openai_response_to_responses(resp)
    assert [o["type"] for o in out["output"]] == ["message", "function_call"]


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
    out = openai_response_to_responses(resp)
    args = out["output"][0]["arguments"]
    assert json.loads(args) == {"a": 1}


def test_response_length_finish_marks_incomplete():
    resp = {
        "choices": [{"finish_reason": "length", "message": {"content": "truncated"}}],
    }
    out = openai_response_to_responses(resp)
    assert out["status"] == "incomplete"
    assert out["incomplete_details"] == {"reason": "max_output_tokens"}


# ---------------------------------------------------------------------------
# End-to-end through the proxy (demo mode)
# ---------------------------------------------------------------------------


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def test_responses_endpoint_basic_text(demo_client):
    r = demo_client.post("/v1/responses", json={"model": "gpt-4o", "input": "hello"})
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "response"
    assert data["status"] == "completed"
    assert isinstance(data["output_text"], str)


def test_responses_endpoint_tool_roundtrip_and_savings(demo_client):
    body = {
        "model": "gpt-4o",
        "input": "where is my order?",
        "tools": [
            {
                "type": "function",
                "name": "get_order",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    }
    r = demo_client.post("/v1/responses", json=body)
    assert r.status_code == 200
    data = r.json()

    # The mock answers with a text summary after the tool round-trip.
    assert any(o["type"] == "message" for o in data["output"])
    assert data["output_text"]
    assert "input_tokens" in data["usage"]

    summary = demo_client.get("/v1/savings/summary").json()
    assert summary["total_calls"] >= 1
