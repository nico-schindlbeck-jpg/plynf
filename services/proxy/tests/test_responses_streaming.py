# SPDX-License-Identifier: Apache-2.0
"""OpenAI *Responses* API streaming (``/v1/responses`` with ``stream: true``).

The Responses stream is a typed-event taxonomy (``response.created`` →
``response.output_text.delta`` / ``response.function_call_arguments.delta`` →
``response.completed``), not chat ``chunk`` deltas. Plynf computes the result
unary (tool-call interception must finish first) and replays the shaped final
body as that event sequence, so a Responses client streaming through Plynf
needs no code change and the per-call savings headers ride along.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import _synthesize_responses_sse, create_app
from plinth_proxy.settings import ProxySettings

_RESPONSES_TOOL_BODY = {
    "model": "gpt-4o",
    "input": "where is my order?",
    "tools": [{"type": "function", "name": "get_order", "parameters": {"type": "object"}}],
}


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def _events(text: str) -> list[tuple[str | None, dict]]:
    """Parse ``event: <type>`` / ``data: {json}`` SSE blocks."""
    out: list[tuple[str | None, dict]] = []
    for block in text.split("\n\n"):
        etype: str | None = None
        data: dict | None = None
        for line in block.splitlines():
            if line.startswith("event:"):
                etype = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:") :].strip())
        if data is not None:
            out.append((etype, data))
    return out


def test_stream_brackets_with_created_and_completed(demo_client):
    body = dict(_RESPONSES_TOOL_BODY, stream=True)
    r = demo_client.post("/v1/responses", json=body)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"

    evs = _events(r.text)
    types = [t for t, _ in evs]
    assert types[0] == "response.created"
    assert types[-1] == "response.completed"
    # The created envelope is in-progress with no output yet; completed is full.
    created = next(d for t, d in evs if t == "response.created")
    assert created["response"]["status"] == "in_progress"
    assert created["response"]["output"] == []
    completed = next(d for t, d in evs if t == "response.completed")
    assert completed["response"]["status"] == "completed"


def test_stream_text_deltas_reconstruct_unary_output(demo_client):
    # The aggregated output_text.delta fragments must equal the unary
    # response's output_text — the stream is the same shaped result, reframed.
    unary = demo_client.post("/v1/responses", json=_RESPONSES_TOOL_BODY)
    assert unary.status_code == 200
    unary_text = unary.json()["output_text"]

    r = demo_client.post("/v1/responses", json=dict(_RESPONSES_TOOL_BODY, stream=True))
    evs = _events(r.text)
    deltas = "".join(d["delta"] for t, d in evs if t == "response.output_text.delta")
    assert deltas == unary_text
    completed = next(d for t, d in evs if t == "response.completed")
    assert completed["response"]["output_text"] == unary_text


def test_stream_sequence_numbers_are_contiguous(demo_client):
    r = demo_client.post("/v1/responses", json=dict(_RESPONSES_TOOL_BODY, stream=True))
    evs = _events(r.text)
    seqs = [d["sequence_number"] for _, d in evs]
    assert seqs == list(range(len(seqs)))


def test_stream_carries_savings_and_request_id(demo_client):
    r = demo_client.post("/v1/responses", json=dict(_RESPONSES_TOOL_BODY, stream=True))
    assert int(r.headers["x-plynf-tool-calls"]) >= 1
    assert r.headers["x-request-id"].startswith("req_")


def test_non_stream_still_returns_json(demo_client):
    r = demo_client.post("/v1/responses", json=_RESPONSES_TOOL_BODY)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["object"] == "response"


async def test_function_call_item_streams_argument_events():
    # The mock always resolves get_order server-side, so the final body never
    # contains an unresolved function_call item — drive the synthesizer directly
    # to cover the tool-call branch deterministically.
    final = {
        "id": "resp_x",
        "object": "response",
        "created_at": 0,
        "model": "gpt-4o",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "get_order",
                "arguments": '{"order_id":"1"}',
                "status": "completed",
            }
        ],
        "output_text": "",
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }
    chunks = [c async for c in _synthesize_responses_sse(final)]
    evs = _events("".join(chunks))
    assert [t for t, _ in evs] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ]
    # The arguments stream whole, then the completed envelope carries the full
    # final body (with sequence numbers contiguous from 0).
    args_delta = next(d for t, d in evs if t == "response.function_call_arguments.delta")
    assert args_delta["delta"] == '{"order_id":"1"}'
    last_type, last_data = evs[-1]
    assert last_type == "response.completed"
    assert last_data["response"] == final
    assert [d["sequence_number"] for _, d in evs] == list(range(len(evs)))
