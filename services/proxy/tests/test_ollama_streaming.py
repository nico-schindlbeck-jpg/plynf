# SPDX-License-Identifier: Apache-2.0
"""Ollama native /api/chat streaming (newline-delimited JSON).

Ollama streaming is NDJSON, not SSE: a run of ``{message:{content},done:false}``
lines followed by a single ``done:true`` line carrying ``done_reason`` and token
counts. Plynf computes the result unary (tool-call interception must finish
first) and replays the shaped final message as NDJSON, so a native client
streaming through Plynf needs no code change and the savings headers ride along.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import (
    _synthesize_ollama_generate_ndjson,
    _synthesize_ollama_ndjson,
    create_app,
)
from plinth_proxy.settings import ProxySettings


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def _lines(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def test_stream_defaults_on_when_omitted(demo_client):
    # Ollama defaults stream=true; omitting it must yield NDJSON, not JSON.
    r = demo_client.post(
        "/api/chat",
        json={"model": "llama3.2", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    lines = _lines(r.text)
    assert lines[-1]["done"] is True
    assert all(ln["done"] is False for ln in lines[:-1])


def test_stream_text_reconstructs_unary_message(demo_client):
    body = {"model": "llama3.2", "messages": [{"role": "user", "content": "hello"}]}
    unary = demo_client.post("/api/chat", json=dict(body, stream=False)).json()
    r = demo_client.post("/api/chat", json=dict(body, stream=True))
    streamed = "".join(ln["message"].get("content", "") for ln in _lines(r.text))
    assert streamed == unary["message"]["content"]


def test_stream_final_line_carries_counts_and_done_reason(demo_client):
    r = demo_client.post(
        "/api/chat",
        json={
            "model": "m",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    last = _lines(r.text)[-1]
    assert last["done"] is True
    assert last["done_reason"] == "stop"
    assert "prompt_eval_count" in last
    assert "eval_count" in last


def test_stream_carries_savings_headers(demo_client):
    r = demo_client.post(
        "/api/chat",
        json={
            "model": "m",
            "stream": True,
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


async def test_synthesize_emits_tool_calls_on_final_line():
    # The mock resolves tools server-side, so drive the synthesizer directly to
    # cover the tool-call branch deterministically.
    final = {
        "model": "llama3.2",
        "created_at": "2026-01-01T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "get_order", "arguments": {"id": 1}}}],
        },
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 3,
        "eval_count": 5,
    }
    lines = [json.loads(c) async for c in _synthesize_ollama_ndjson(final)]
    assert len(lines) == 1  # no content → only the terminal line
    assert lines[0]["done"] is True
    assert lines[0]["message"]["tool_calls"][0]["function"]["name"] == "get_order"
    assert lines[0]["eval_count"] == 5


# ---------------------------------------------------------------------------
# /api/generate streaming (flat `response` NDJSON)
# ---------------------------------------------------------------------------


def test_generate_stream_defaults_on_and_reconstructs(demo_client):
    body = {"model": "llama3.2", "prompt": "why is the sky blue?"}
    unary = demo_client.post("/api/generate", json=dict(body, stream=False)).json()
    r = demo_client.post("/api/generate", json=body)  # stream omitted → NDJSON
    assert r.headers["content-type"].startswith("application/x-ndjson")
    lines = _lines(r.text)
    assert lines[-1]["done"] is True
    streamed = "".join(ln.get("response", "") for ln in lines)
    assert streamed == unary["response"]


async def test_synthesize_generate_final_line_carries_counts():
    final = {
        "model": "llama3.2",
        "created_at": "2026-01-01T00:00:00Z",
        "response": "blue light scatters",
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 6,
        "eval_count": 3,
    }
    lines = [json.loads(c) async for c in _synthesize_ollama_generate_ndjson(final)]
    assert "".join(ln.get("response", "") for ln in lines) == "blue light scatters"
    assert lines[-1]["done"] is True
    assert lines[-1]["done_reason"] == "stop"
    assert lines[-1]["eval_count"] == 3
    assert all("message" not in ln for ln in lines)  # flat form, never wrapped
