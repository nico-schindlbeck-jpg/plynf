# SPDX-License-Identifier: Apache-2.0
"""Tests for the API endpoints — streaming + non-streaming + savings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings

FIXTURES = Path(__file__).resolve().parent.parent.parent.parent / "examples" / "customer-support"


@pytest.fixture
def app():
    settings = ProxySettings(demo_mode=True)
    return create_app(settings)


@pytest.fixture
def client(app):
    return TestClient(app)


def _demo_body(stream: bool = False) -> dict:
    body = json.loads((FIXTURES / "demo_request.json").read_text(encoding="utf-8"))
    if stream:
        body["stream"] = True
    return body


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["demo_mode"] is True


def test_non_streaming_returns_full_completion(client):
    r = client.post("/v1/chat/completions", json=_demo_body())
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    content = body["choices"][0]["message"]["content"]
    # The mock LLM should answer about the order.
    assert "12345" in content
    assert "DHL" in content


def test_streaming_returns_sse(client):
    r = client.post("/v1/chat/completions", json=_demo_body(stream=True))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    # Collect SSE chunks.
    raw = r.text
    lines = [line for line in raw.splitlines() if line.startswith("data:")]
    assert lines, "expected at least one SSE data line"
    # Last data line should be [DONE].
    assert lines[-1] == "data: [DONE]"
    # The first chunk should carry the role.
    first = json.loads(lines[0][len("data: "):])
    assert first["choices"][0]["delta"]["role"] == "assistant"
    # Some chunk between must carry content (the tool-shaped answer).
    contents = []
    for line in lines[1:-1]:
        chunk = json.loads(line[len("data: "):])
        delta = chunk["choices"][0]["delta"]
        if "content" in delta:
            contents.append(delta["content"])
    joined = "".join(contents)
    assert "12345" in joined
    assert "DHL" in joined


def test_savings_summary_aggregates_after_call(client):
    client.post("/v1/chat/completions", json=_demo_body())
    summary = client.get("/v1/savings/summary").json()
    assert summary["total_calls"] >= 1
    assert summary["total_saved_tokens"] > 0
    assert summary["savings_pct"] > 0.5  # mock fixture yields ~96%


def test_policies_endpoint_lists_loaded_connectors(client):
    r = client.get("/v1/policies").json()
    names = {c["connector"] for c in r["connectors"]}
    assert {"salesforce", "orders", "slack"} <= names


def test_streaming_on_cache_hit_still_emits_done(client):
    # Hit the same request twice; second should be cached but stream should still complete.
    client.post("/v1/chat/completions", json=_demo_body())
    r = client.post("/v1/chat/completions", json=_demo_body(stream=True))
    assert r.status_code == 200
    assert r.text.rstrip().endswith("data: [DONE]")
