# SPDX-License-Identifier: Apache-2.0
"""Ollama native front door: /api/chat + /api/tags + /api/version.

Covers the two-direction translation (Ollama ⇄ OpenAI) and the endpoint
integration in demo mode. Streaming (NDJSON) lives in test_ollama_streaming.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.ollama_adapter import (
    ollama_chat_request_to_openai,
    ollama_embeddings_request_to_openai,
    ollama_generate_request_to_openai,
    ollama_tags_from_models,
    openai_embeddings_to_ollama_embed,
    openai_embeddings_to_ollama_legacy,
    openai_response_to_ollama_chat,
    openai_response_to_ollama_generate,
)
from plinth_proxy.settings import ProxySettings

# ---------------------------------------------------------------------------
# Request translation: Ollama → OpenAI
# ---------------------------------------------------------------------------


def test_request_maps_options_to_openai_knobs():
    body = {
        "model": "llama3.2",
        "messages": [{"role": "user", "content": "hi"}],
        "options": {
            "temperature": 0.5,
            "top_p": 0.9,
            "num_predict": 128,
            "stop": ["X"],
            "seed": 7,
        },
    }
    out = ollama_chat_request_to_openai(body)
    assert out["model"] == "llama3.2"
    assert out["messages"] == [{"role": "user", "content": "hi"}]
    assert out["temperature"] == 0.5
    assert out["top_p"] == 0.9
    assert out["max_tokens"] == 128  # num_predict
    assert out["stop"] == ["X"]
    assert out["seed"] == 7


def test_request_format_json_maps_to_response_format():
    out = ollama_chat_request_to_openai({"model": "m", "messages": [], "format": "json"})
    assert out["response_format"] == {"type": "json_object"}


def test_request_format_schema_maps_to_json_schema():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    out = ollama_chat_request_to_openai({"model": "m", "messages": [], "format": schema})
    assert out["response_format"]["type"] == "json_schema"
    assert out["response_format"]["json_schema"]["schema"] == schema


def test_request_tool_calls_object_args_become_json_string():
    body = {
        "model": "m",
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "get_order", "arguments": {"id": 1}}}
                ],
            },
            {"role": "tool", "tool_name": "get_order", "content": "{}"},
        ],
    }
    out = ollama_chat_request_to_openai(body)
    tc = out["messages"][0]["tool_calls"][0]
    assert tc["type"] == "function"
    assert tc["id"]  # synthesized when Ollama omits it
    assert tc["function"]["arguments"] == '{"id": 1}'  # object → JSON string
    tool_msg = out["messages"][1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "get_order"  # falls back to tool_name


# ---------------------------------------------------------------------------
# Response translation: OpenAI → Ollama
# ---------------------------------------------------------------------------


def test_response_maps_to_ollama_shape():
    resp = {
        "choices": [
            {"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 4},
        "created": 1700000000,
    }
    out = openai_response_to_ollama_chat(resp, model="llama3.2")
    assert out["model"] == "llama3.2"
    assert out["message"] == {"role": "assistant", "content": "hello"}
    assert out["done"] is True
    assert out["done_reason"] == "stop"
    assert out["prompt_eval_count"] == 11
    assert out["eval_count"] == 4
    assert out["created_at"].endswith("Z")  # RFC3339 from the unix `created`


def test_response_length_finish_maps_to_done_reason_length():
    resp = {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}
    assert openai_response_to_ollama_chat(resp)["done_reason"] == "length"


def test_response_tool_calls_args_decoded_to_object():
    resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "get_order",
                                "arguments": '{"order_id":"42"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    out = openai_response_to_ollama_chat(resp)
    tc = out["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "get_order"
    assert tc["function"]["arguments"] == {"order_id": "42"}  # JSON string → object
    assert out["done_reason"] == "stop"  # tool_calls maps to "stop" in Ollama
    assert out["message"]["content"] == ""  # None coerced to empty string


# ---------------------------------------------------------------------------
# /api/generate translation (single-turn prompt)
# ---------------------------------------------------------------------------


def test_generate_request_builds_messages_from_system_and_prompt():
    out = ollama_generate_request_to_openai(
        {"model": "llama3.2", "system": "be brief", "prompt": "why is the sky blue?"}
    )
    assert out["model"] == "llama3.2"
    assert out["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "why is the sky blue?"},
    ]


def test_generate_request_without_system_is_single_user_turn():
    out = ollama_generate_request_to_openai({"model": "m", "prompt": "hi"})
    assert out["messages"] == [{"role": "user", "content": "hi"}]


def test_generate_request_applies_format_and_options():
    out = ollama_generate_request_to_openai(
        {"model": "m", "prompt": "x", "format": "json", "options": {"num_predict": 64}}
    )
    assert out["response_format"] == {"type": "json_object"}
    assert out["max_tokens"] == 64


def test_generate_response_is_flat_response_string():
    resp = {
        "choices": [{"message": {"content": "blue light scatters"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 6, "completion_tokens": 3},
    }
    out = openai_response_to_ollama_generate(resp, model="llama3.2")
    assert out["response"] == "blue light scatters"
    assert "message" not in out  # flat, not wrapped
    assert out["done"] is True
    assert out["done_reason"] == "stop"
    assert out["prompt_eval_count"] == 6
    assert out["eval_count"] == 3


# ---------------------------------------------------------------------------
# Embeddings translation
# ---------------------------------------------------------------------------


def test_embeddings_request_prefers_input_else_prompt():
    # /api/embed sends `input`; legacy /api/embeddings sends `prompt`.
    assert ollama_embeddings_request_to_openai(
        {"model": "m", "input": ["a", "b"]}
    ) == {"model": "m", "input": ["a", "b"]}
    assert ollama_embeddings_request_to_openai(
        {"model": "m", "prompt": "hello"}
    ) == {"model": "m", "input": "hello"}


def test_embeddings_response_legacy_single_vector():
    resp = {"object": "list", "data": [{"index": 0, "embedding": [0.1, 0.2]}]}
    assert openai_embeddings_to_ollama_legacy(resp) == {"embedding": [0.1, 0.2]}


def test_embeddings_response_embed_list_of_vectors():
    resp = {
        "object": "list",
        "data": [
            {"index": 0, "embedding": [0.1]},
            {"index": 1, "embedding": [0.2]},
        ],
        "usage": {"prompt_tokens": 5},
    }
    out = openai_embeddings_to_ollama_embed(resp, model="m")
    assert out["model"] == "m"
    assert out["embeddings"] == [[0.1], [0.2]]
    assert out["prompt_eval_count"] == 5


# ---------------------------------------------------------------------------
# Model listing: OpenAI ListModels → Ollama /api/tags
# ---------------------------------------------------------------------------


def test_tags_reshapes_listmodels():
    catalog = {
        "object": "list",
        "data": [
            {"id": "gpt-4o", "owned_by": "openai"},
            {"id": "groq/llama-3.3-70b", "owned_by": "plynf-proxy"},
            {"not": "a model"},  # no id → skipped
        ],
    }
    out = ollama_tags_from_models(catalog)
    assert [m["name"] for m in out["models"]] == ["gpt-4o", "groq/llama-3.3-70b"]
    assert out["models"][0]["model"] == "gpt-4o"
    assert out["models"][0]["details"]["family"] == "openai"


# ---------------------------------------------------------------------------
# Endpoint integration (demo mode)
# ---------------------------------------------------------------------------


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def test_api_chat_non_stream_returns_ollama_shape(demo_client):
    r = demo_client.post(
        "/api/chat",
        json={
            "model": "llama3.2",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        },
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["model"] == "llama3.2"  # echoes the caller's model
    assert body["message"]["role"] == "assistant"
    assert body["done"] is True
    assert r.headers["x-request-id"].startswith("req_")


def test_api_chat_tool_roundtrip_sets_savings_headers(demo_client):
    r = demo_client.post(
        "/api/chat",
        json={
            "model": "llama3.2",
            "stream": False,
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
        },
    )
    assert r.status_code == 200
    assert int(r.headers["x-plynf-tool-calls"]) >= 1
    assert int(r.headers["x-plynf-raw-tokens"]) >= int(r.headers["x-plynf-shaped-tokens"])


def test_api_tags_lists_models(demo_client):
    r = demo_client.get("/api/tags")
    assert r.status_code == 200
    names = {m["name"] for m in r.json()["models"]}
    assert "gpt-4o" in names  # from the mock catalog


def test_api_version(demo_client):
    r = demo_client.get("/api/version")
    assert r.status_code == 200
    assert r.json()["version"].startswith("0.0.0-plynf")


def test_api_generate_non_stream_returns_flat_response(demo_client):
    r = demo_client.post(
        "/api/generate",
        json={"model": "llama3.2", "prompt": "why is the sky blue?", "stream": False},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["model"] == "llama3.2"
    assert isinstance(body["response"], str) and body["response"]
    assert "message" not in body  # /api/generate is flat
    assert body["done"] is True


def test_api_embeddings_legacy_returns_single_vector(demo_client):
    r = demo_client.post(
        "/api/embeddings", json={"model": "nomic-embed-text", "prompt": "embed me"}
    )
    assert r.status_code == 200
    body = r.json()
    assert "embedding" in body
    assert isinstance(body["embedding"], list) and len(body["embedding"]) == 16  # mock dim


def test_api_embed_returns_list_of_vectors(demo_client):
    r = demo_client.post(
        "/api/embed", json={"model": "nomic-embed-text", "input": ["a", "b", "c"]}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "nomic-embed-text"
    assert len(body["embeddings"]) == 3  # one vector per input
    assert all(len(v) == 16 for v in body["embeddings"])
