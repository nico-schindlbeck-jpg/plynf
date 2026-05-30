# SPDX-License-Identifier: Apache-2.0
"""Tests for the Gemini adapter + the public-Gemini and Vertex AI front doors.

The Vertex path reuses the Gemini translators verbatim, so these tests also
guard the shared ``_run_gemini_dialect`` helper both endpoints call.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.gemini_adapter import (
    gemini_request_to_openai,
    openai_response_to_gemini,
)
from plinth_proxy.settings import ProxySettings

# ---------------------------------------------------------------------------
# Request translation: Gemini → OpenAI
# ---------------------------------------------------------------------------


def test_request_system_instruction_and_contents():
    out = gemini_request_to_openai(
        {
            "systemInstruction": {"parts": [{"text": "be brief"}]},
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        },
        model="gpt-4o",
    )
    assert out["messages"][0] == {"role": "system", "content": "be brief"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_request_model_role_maps_to_assistant():
    out = gemini_request_to_openai(
        {"contents": [{"role": "model", "parts": [{"text": "prior answer"}]}]}
    )
    assert out["messages"][0] == {"role": "assistant", "content": "prior answer"}


def test_request_function_call_and_response_roundtrip():
    body = {
        "contents": [
            {"role": "user", "parts": [{"text": "where is order 12345?"}]},
            {
                "role": "model",
                "parts": [
                    {"functionCall": {"name": "get_order", "args": {"order_id": "12345"}}}
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "get_order",
                            "response": {"status": "shipped"},
                        }
                    }
                ],
            },
        ]
    }
    out = gemini_request_to_openai(body)
    msgs = out["messages"]

    assert msgs[0] == {"role": "user", "content": "where is order 12345?"}

    assistant = msgs[1]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["function"]["name"] == "get_order"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {
        "order_id": "12345"
    }

    tool_msg = msgs[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["name"] == "get_order"
    assert "shipped" in tool_msg["content"]


def test_request_function_declarations_to_tools():
    out = gemini_request_to_openai(
        {
            "contents": [],
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": "get_order",
                            "description": "Look up an order",
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ]
                }
            ],
        }
    )
    assert out["tools"][0]["type"] == "function"
    assert out["tools"][0]["function"]["name"] == "get_order"


def test_request_generation_config_knobs():
    out = gemini_request_to_openai(
        {
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 128, "temperature": 0.2},
        }
    )
    assert out["max_tokens"] == 128
    assert out["temperature"] == 0.2


# ---------------------------------------------------------------------------
# Response translation: OpenAI → Gemini
# ---------------------------------------------------------------------------


def test_response_text_candidate():
    resp = {
        "model": "gemini-1.5-pro",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Hello there"},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }
    out = openai_response_to_gemini(resp)
    cand = out["candidates"][0]
    assert cand["content"]["role"] == "model"
    assert cand["content"]["parts"] == [{"text": "Hello there"}]
    assert cand["finishReason"] == "STOP"
    assert out["usageMetadata"]["promptTokenCount"] == 10
    assert out["usageMetadata"]["candidatesTokenCount"] == 3
    assert out["usageMetadata"]["totalTokenCount"] == 13


def test_response_tool_call_to_function_call_part():
    resp = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
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
    }
    out = openai_response_to_gemini(resp)
    part = out["candidates"][0]["content"]["parts"][0]
    assert part["functionCall"]["name"] == "get_order"
    assert part["functionCall"]["args"] == {"order_id": "1"}
    # Gemini has no dedicated tool stop reason — it folds into STOP.
    assert out["candidates"][0]["finishReason"] == "STOP"


@pytest.mark.parametrize(
    "openai_reason,gemini_reason",
    [
        ("stop", "STOP"),
        ("tool_calls", "STOP"),
        ("length", "MAX_TOKENS"),
        ("content_filter", "SAFETY"),
        ("unknown", "STOP"),
    ],
)
def test_response_finish_reason_map(openai_reason, gemini_reason):
    resp = {"choices": [{"finish_reason": openai_reason, "message": {"content": "x"}}]}
    assert openai_response_to_gemini(resp)["candidates"][0]["finishReason"] == gemini_reason


# ---------------------------------------------------------------------------
# End-to-end front doors (demo mode)
# ---------------------------------------------------------------------------


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


_ORDER_BODY = {
    "contents": [{"role": "user", "parts": [{"text": "where is my order?"}]}],
    "tools": [
        {
            "functionDeclarations": [
                {"name": "get_order", "parameters": {"type": "object", "properties": {}}}
            ]
        }
    ],
}


def test_gemini_endpoint_tool_roundtrip_and_savings(demo_client):
    r = demo_client.post(
        "/v1beta/models/gemini-1.5-pro:generateContent", json=_ORDER_BODY
    )
    assert r.status_code == 200
    cand = r.json()["candidates"][0]
    assert cand["content"]["role"] == "model"
    assert any("text" in p for p in cand["content"]["parts"])

    summary = demo_client.get("/v1/savings/summary").json()
    assert summary["total_calls"] >= 1


def test_vertex_endpoint_reuses_gemini_translation(demo_client):
    path = (
        "/v1/projects/my-proj/locations/us-central1"
        "/publishers/google/models/gemini-1.5-pro:generateContent"
    )
    r = demo_client.post(path, json=_ORDER_BODY)
    assert r.status_code == 200
    body = r.json()
    assert body["candidates"][0]["content"]["role"] == "model"
    assert "usageMetadata" in body
