# SPDX-License-Identifier: Apache-2.0
"""Cohere v2 chat streaming (``POST /v2/chat`` with ``stream: true``).

Cohere v2 streaming is a typed-event taxonomy carried as ``data: {json}`` SSE,
with the event kind in each payload's ``type`` field (``message-start`` →
``content-start`` → ``content-delta`` → ``content-end`` → ``message-end``).
Plynf computes the result unary (tool-call interception must finish first) and
replays the shaped final message as that event sequence, so a Cohere client
streaming through Plynf needs no code change and the per-call savings headers
ride along.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import _synthesize_cohere_sse, create_app
from plinth_proxy.settings import ProxySettings

_COHERE_TOOL_BODY = {
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

_COHERE_PLAIN_BODY = {
    "model": "command-r-plus",
    "messages": [{"role": "user", "content": "hello"}],
}


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def _events(text: str) -> list[dict]:
    """Parse ``data: {json}`` SSE blocks into a list of decoded payloads."""
    out: list[dict] = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[len("data:") :].strip()))
    return out


def _agg_text(events: list[dict]) -> str:
    return "".join(
        e["delta"]["message"]["content"]["text"]
        for e in events
        if e.get("type") == "content-delta"
    )


def test_stream_brackets_with_message_start_and_end(demo_client):
    body = dict(_COHERE_TOOL_BODY, stream=True)
    r = demo_client.post("/v2/chat", json=body)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"

    evs = _events(r.text)
    types = [e["type"] for e in evs]
    assert types[0] == "message-start"
    assert types[-1] == "message-end"
    # The closing event carries the finish_reason and usage.
    end = evs[-1]
    assert end["delta"]["finish_reason"] == "COMPLETE"
    assert "tokens" in end["delta"]["usage"]


def test_stream_text_deltas_reconstruct_unary_message(demo_client):
    # The aggregated content-delta fragments must equal the unary response's
    # assistant text — the stream is the same shaped result, reframed.
    unary = demo_client.post("/v2/chat", json=_COHERE_TOOL_BODY)
    assert unary.status_code == 200
    unary_text = "".join(
        b.get("text", "")
        for b in unary.json()["message"]["content"]
        if b.get("type") == "text"
    )

    r = demo_client.post("/v2/chat", json=dict(_COHERE_TOOL_BODY, stream=True))
    evs = _events(r.text)
    assert _agg_text(evs) == unary_text
    # content-start / content-end bracket the text block.
    types = [e["type"] for e in evs]
    assert "content-start" in types
    assert "content-end" in types


def test_stream_carries_savings_and_request_id(demo_client):
    r = demo_client.post("/v2/chat", json=dict(_COHERE_TOOL_BODY, stream=True))
    assert r.status_code == 200
    # The get_order round-trip was intercepted → savings headers are non-trivial.
    assert int(r.headers["x-plynf-tool-calls"]) >= 1
    assert int(r.headers["x-plynf-raw-tokens"]) >= int(r.headers["x-plynf-shaped-tokens"])
    assert r.headers["x-request-id"].startswith("req_")


def test_stream_plain_text(demo_client):
    r = demo_client.post("/v2/chat", json=dict(_COHERE_PLAIN_BODY, stream=True))
    assert r.status_code == 200
    evs = _events(r.text)
    assert _agg_text(evs)  # non-empty assistant text streamed word-by-word
    assert evs[-1]["type"] == "message-end"


def test_non_stream_still_returns_json(demo_client):
    r = demo_client.post("/v2/chat", json=_COHERE_PLAIN_BODY)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["message"]["role"] == "assistant"


async def test_tool_call_message_streams_tool_events():
    # The mock always resolves get_order server-side, so the final message
    # never carries an unresolved tool_call — drive the synthesizer directly
    # to cover the tool-call branch deterministically.
    final = {
        "id": "cohere-x",
        "finish_reason": "TOOL_CALL",
        "message": {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_order", "arguments": '{"order_id":"1"}'},
                }
            ],
        },
        "usage": {
            "billed_units": {"input_tokens": 0, "output_tokens": 0},
            "tokens": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    chunks = [c async for c in _synthesize_cohere_sse(final)]
    evs = _events("".join(chunks))
    assert [e["type"] for e in evs] == [
        "message-start",
        "tool-call-start",
        "tool-call-delta",
        "tool-call-end",
        "message-end",
    ]
    start = next(e for e in evs if e["type"] == "tool-call-start")
    assert start["delta"]["message"]["tool_calls"]["function"]["name"] == "get_order"
    delta = next(e for e in evs if e["type"] == "tool-call-delta")
    assert delta["delta"]["message"]["tool_calls"]["function"]["arguments"] == '{"order_id":"1"}'
    assert evs[-1]["delta"]["finish_reason"] == "TOOL_CALL"
