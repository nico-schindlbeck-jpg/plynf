# SPDX-License-Identifier: Apache-2.0
"""Tests for the AWS Bedrock Converse translation layer + endpoint."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.bedrock_adapter import (
    bedrock_converse_request_to_openai,
    openai_response_to_bedrock_converse,
)
from plinth_proxy.settings import ProxySettings

# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------


def test_simple_user_message_translates():
    body = {
        "messages": [{"role": "user", "content": [{"text": "Hello"}]}],
    }
    openai = bedrock_converse_request_to_openai(body, model="anthropic.claude-3-5-sonnet")
    assert openai["model"] == "anthropic.claude-3-5-sonnet"
    assert openai["messages"] == [{"role": "user", "content": "Hello"}]


def test_bare_string_content_tolerated():
    body = {"messages": [{"role": "user", "content": "Hi there"}]}
    openai = bedrock_converse_request_to_openai(body)
    assert openai["messages"] == [{"role": "user", "content": "Hi there"}]


def test_system_blocks_become_system_message():
    body = {
        "system": [{"text": "Line one."}, {"text": "Line two."}],
        "messages": [{"role": "user", "content": [{"text": "Hi"}]}],
    }
    openai = bedrock_converse_request_to_openai(body)
    assert openai["messages"][0]["role"] == "system"
    assert "Line one." in openai["messages"][0]["content"]
    assert "Line two." in openai["messages"][0]["content"]
    assert openai["messages"][1] == {"role": "user", "content": "Hi"}


def test_system_as_bare_string():
    body = {
        "system": "You are a support agent.",
        "messages": [{"role": "user", "content": [{"text": "Hi"}]}],
    }
    openai = bedrock_converse_request_to_openai(body)
    assert openai["messages"][0] == {
        "role": "system",
        "content": "You are a support agent.",
    }


def test_tool_use_block_becomes_openai_tool_calls():
    body = {
        "messages": [
            {"role": "user", "content": [{"text": "Order 12345?"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tooluse_abc",
                            "name": "get_order",
                            "input": {"order_id": "12345"},
                        }
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tooluse_abc",
                            "content": [{"json": {"status": "in_transit"}}],
                            "status": "success",
                        }
                    }
                ],
            },
        ]
    }
    openai = bedrock_converse_request_to_openai(body)
    roles = [m["role"] for m in openai["messages"]]
    assert "tool" in roles
    asst = next(m for m in openai["messages"] if m.get("tool_calls"))
    tc = asst["tool_calls"][0]
    assert tc["function"]["name"] == "get_order"
    # Converse hands input as an object; we re-encode it as a JSON string.
    assert json.loads(tc["function"]["arguments"]) == {"order_id": "12345"}
    assert tc["id"] == "tooluse_abc"
    # Tool result preserves the toolUseId and flattens the json block.
    tool_msg = next(m for m in openai["messages"] if m.get("role") == "tool")
    assert tool_msg["tool_call_id"] == "tooluse_abc"
    assert json.loads(tool_msg["content"]) == {"status": "in_transit"}


def test_tool_result_text_blocks_flatten():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "t1",
                            "content": [{"text": "hello "}, {"text": "world"}],
                        }
                    }
                ],
            }
        ]
    }
    openai = bedrock_converse_request_to_openai(body)
    tool_msg = next(m for m in openai["messages"] if m.get("role") == "tool")
    assert tool_msg["content"] == "hello world"


def test_tool_definitions_translate():
    body = {
        "messages": [{"role": "user", "content": [{"text": "Hi"}]}],
        "toolConfig": {
            "tools": [
                {
                    "toolSpec": {
                        "name": "get_order",
                        "description": "Fetch order by id",
                        "inputSchema": {
                            "json": {
                                "type": "object",
                                "properties": {"order_id": {"type": "string"}},
                                "required": ["order_id"],
                            }
                        },
                    }
                }
            ]
        },
    }
    openai = bedrock_converse_request_to_openai(body)
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


@pytest.mark.parametrize(
    "choice,expected",
    [
        ({"auto": {}}, "auto"),
        ({"any": {}}, "required"),
        ({"tool": {"name": "get_order"}}, {"type": "function", "function": {"name": "get_order"}}),
        (None, None),
        ({}, None),
    ],
)
def test_tool_choice_translates(choice, expected):
    body = {
        "messages": [{"role": "user", "content": [{"text": "Hi"}]}],
        "toolConfig": {"toolChoice": choice} if choice is not None else {},
    }
    openai = bedrock_converse_request_to_openai(body)
    if expected is None:
        assert "tool_choice" not in openai
    else:
        assert openai["tool_choice"] == expected


def test_inference_config_maps_to_openai_knobs():
    body = {
        "messages": [{"role": "user", "content": [{"text": "Hi"}]}],
        "inferenceConfig": {
            "maxTokens": 512,
            "temperature": 0.3,
            "topP": 0.9,
            "stopSequences": ["\n\n"],
        },
    }
    openai = bedrock_converse_request_to_openai(body)
    assert openai["max_tokens"] == 512
    assert openai["temperature"] == 0.3
    assert openai["top_p"] == 0.9
    assert openai["stop"] == ["\n\n"]


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
    out = openai_response_to_bedrock_converse(openai_resp)
    assert out["output"]["message"]["role"] == "assistant"
    assert out["output"]["message"]["content"] == [{"text": "Hello there."}]
    assert out["stopReason"] == "end_turn"
    assert out["usage"]["inputTokens"] == 10
    assert out["usage"]["outputTokens"] == 4
    assert out["usage"]["totalTokens"] == 14


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
    out = openai_response_to_bedrock_converse(openai_resp)
    assert out["stopReason"] == "tool_use"
    blocks = out["output"]["message"]["content"]
    tu = next(b["toolUse"] for b in blocks if "toolUse" in b)
    assert tu["name"] == "get_order"
    assert tu["input"] == {"order_id": "12345"}
    assert tu["toolUseId"] == "call_xyz"


def test_finish_reason_length_maps_to_max_tokens():
    openai_resp = {
        "choices": [
            {"message": {"role": "assistant", "content": "x"}, "finish_reason": "length"}
        ]
    }
    out = openai_response_to_bedrock_converse(openai_resp)
    assert out["stopReason"] == "max_tokens"


def test_empty_content_yields_placeholder_text_block():
    openai_resp = {
        "choices": [{"message": {"role": "assistant", "content": None}, "finish_reason": "stop"}]
    }
    out = openai_response_to_bedrock_converse(openai_resp)
    assert out["output"]["message"]["content"] == [{"text": ""}]


# ---------------------------------------------------------------------------
# End-to-end via the FastAPI app
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def test_converse_endpoint_returns_bedrock_shape(client):
    request = {
        "system": [{"text": "You are a customer support agent."}],
        "messages": [
            {
                "role": "user",
                "content": [{"text": "What is the status of order 12345?"}],
            }
        ],
        "toolConfig": {
            "tools": [
                {
                    "toolSpec": {
                        "name": "get_order",
                        "description": "Fetch order by id",
                        "inputSchema": {"json": {"type": "object"}},
                    }
                }
            ]
        },
        "inferenceConfig": {"maxTokens": 1024},
    }
    r = client.post("/model/anthropic.claude-3-5-sonnet-20241022-v2:0/converse", json=request)
    assert r.status_code == 200
    body = r.json()
    assert body["output"]["message"]["role"] == "assistant"
    assert isinstance(body["output"]["message"]["content"], list)
    assert "stopReason" in body
    assert "usage" in body
    text = "".join(
        b.get("text", "") for b in body["output"]["message"]["content"] if "text" in b
    )
    assert "12345" in text


def test_converse_endpoint_emits_savings(client):
    client.post(
        "/model/anthropic.claude-3-5-sonnet/converse",
        json={
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
        },
    )
    summary = client.get("/v1/savings/summary").json()
    assert summary["total_calls"] >= 1
    assert summary["total_saved_tokens"] > 0
