# SPDX-License-Identifier: Apache-2.0
"""Gemini ``streamGenerateContent`` streaming.

Gemini's streaming method frames output as SSE (``data: {...}``) when the
client passes ``?alt=sse`` — what the google-genai SDK requests — and as a JSON
array of responses otherwise. Plynf computes the result unary (tool-call
interception must finish first) and re-emits it in the requested framing, so a
Gemini client streaming through Plynf needs no code change and the per-call
savings headers ride along.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings

_GEMINI_TOOL_BODY = {
    "contents": [{"role": "user", "parts": [{"text": "where is my order?"}]}],
    "tools": [
        {"functionDeclarations": [{"name": "get_order", "parameters": {"type": "object"}}]}
    ],
}

_GEMINI_PLAIN_BODY = {
    "contents": [{"role": "user", "parts": [{"text": "hello"}]}]
}

_PUBLIC_STREAM = "/v1beta/models/gemini-1.5-pro:streamGenerateContent"
_VERTEX_STREAM = (
    "/v1/projects/p/locations/us-central1/publishers/google/models/"
    "gemini-1.5-pro:streamGenerateContent"
)


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def _sse_chunks(text: str) -> list[dict]:
    """Parse ``data: {json}`` SSE blocks into a list of decoded payloads."""
    chunks: list[dict] = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                chunks.append(json.loads(line[len("data:") :].strip()))
    return chunks


def _agg_text(chunks: list[dict]) -> str:
    out: list[str] = []
    for ch in chunks:
        for cand in ch.get("candidates", []):
            for part in (cand.get("content") or {}).get("parts", []):
                if "text" in part:
                    out.append(part["text"])
    return "".join(out)


def test_sse_stream_matches_unary_and_terminates(demo_client):
    # The aggregated streamed text must equal the unary response's text — the
    # stream is the same shaped result, just reframed.
    unary = demo_client.post(
        "/v1beta/models/gemini-1.5-pro:generateContent", json=_GEMINI_TOOL_BODY
    )
    assert unary.status_code == 200
    unary_text = unary.json()["candidates"][0]["content"]["parts"][0]["text"]

    r = demo_client.post(_PUBLIC_STREAM + "?alt=sse", json=_GEMINI_TOOL_BODY)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"

    chunks = _sse_chunks(r.text)
    assert chunks
    assert _agg_text(chunks) == unary_text
    # Terminal chunk carries finishReason + usageMetadata.
    last = chunks[-1]
    assert last["candidates"][0]["finishReason"] == "STOP"
    assert "usageMetadata" in last


def test_sse_stream_carries_savings_and_request_id(demo_client):
    r = demo_client.post(_PUBLIC_STREAM + "?alt=sse", json=_GEMINI_TOOL_BODY)
    assert r.status_code == 200
    # The get_order round-trip was intercepted → savings headers are non-trivial.
    assert int(r.headers["x-plynf-tool-calls"]) >= 1
    assert int(r.headers["x-plynf-raw-tokens"]) >= int(r.headers["x-plynf-shaped-tokens"])
    assert r.headers["x-request-id"].startswith("req_")


def test_sse_stream_plain_text(demo_client):
    r = demo_client.post(_PUBLIC_STREAM + "?alt=sse", json=_GEMINI_PLAIN_BODY)
    assert r.status_code == 200
    chunks = _sse_chunks(r.text)
    assert _agg_text(chunks)  # non-empty assistant text streamed word-by-word
    assert chunks[-1]["candidates"][0]["finishReason"] == "STOP"


def test_non_sse_returns_json_array(demo_client):
    # Without ?alt=sse, Gemini streaming returns a JSON array of responses;
    # Plynf returns the aggregated final candidate as a one-element array.
    r = demo_client.post(_PUBLIC_STREAM, json=_GEMINI_PLAIN_BODY)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert "candidates" in data[0]


def test_vertex_stream_sse(demo_client):
    r = demo_client.post(_VERTEX_STREAM + "?alt=sse", json=_GEMINI_PLAIN_BODY)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    chunks = _sse_chunks(r.text)
    assert chunks[-1]["candidates"][0]["finishReason"] == "STOP"
