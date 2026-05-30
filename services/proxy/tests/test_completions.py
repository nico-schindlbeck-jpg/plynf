# SPDX-License-Identifier: Apache-2.0
"""The legacy OpenAI ``/v1/completions`` endpoint.

Some clients (LangChain's ``OpenAI`` LLM class, llama-index, older scripts)
still hit the pre-chat completions endpoint. Plynf shapes nothing there (it
predates tool-calling), so — like ``/v1/embeddings`` — it gates the tenant but
doesn't charge, and falls back to a deterministic mock in demo / offline mode
so a client pointed entirely at Plynf keeps working rather than 404ing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def test_string_prompt_returns_text_completion(demo_client):
    r = demo_client.post(
        "/v1/completions", json={"model": "gpt-3.5-turbo-instruct", "prompt": "Say hi"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "text_completion"
    assert body["model"] == "gpt-3.5-turbo-instruct"
    assert len(body["choices"]) == 1
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["finish_reason"] == "stop"
    assert choice["logprobs"] is None
    assert isinstance(choice["text"], str) and choice["text"]
    usage = body["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_list_prompt_yields_one_choice_per_prompt(demo_client):
    r = demo_client.post("/v1/completions", json={"prompt": ["alpha", "beta", "gamma"]})
    assert r.status_code == 200
    choices = r.json()["choices"]
    assert [c["index"] for c in choices] == [0, 1, 2]
    # Each prompt gets its own deterministic continuation.
    assert all(c["text"] for c in choices)


def test_defaults_model_when_omitted(demo_client):
    r = demo_client.post("/v1/completions", json={"prompt": "hello"})
    assert r.status_code == 200
    assert r.json()["model"] == "gpt-3.5-turbo-instruct"


def test_empty_prompt_still_returns_a_choice(demo_client):
    # No prompt at all → one choice, no crash (mock must never 500).
    r = demo_client.post("/v1/completions", json={})
    assert r.status_code == 200
    assert len(r.json()["choices"]) == 1


def test_completions_are_not_charged_as_savings(demo_client):
    # Legacy completions aren't shaped, so they must emit no SavingsEvent and
    # leave the tenant's monthly usage untouched (mirrors /v1/embeddings).
    st = demo_client.app.state.plinth
    before_events = len(st.events)
    before_usage = st.gate.usage("demo")
    r = demo_client.post("/v1/completions", json={"prompt": "anything"})
    assert r.status_code == 200
    assert len(st.events) == before_events
    assert st.gate.usage("demo") == before_usage


def test_over_budget_free_tenant_blocked_with_openai_envelope():
    # Gated like every other generation path: an exhausted free tenant gets a
    # 402, reshaped into OpenAI's error envelope (the /v1/completions path is
    # the OpenAI dialect).
    app = create_app(
        ProxySettings(demo_mode=True, api_keys="tenant-a:key-a:free")
    )
    client = TestClient(app)
    app.state.plinth.gate.record_tokens("tenant-a", 200_000)
    r = client.post(
        "/v1/completions",
        headers={"Authorization": "Bearer key-a"},
        json={"prompt": "hi"},
    )
    assert r.status_code == 402
    err = r.json()["error"]
    assert err["type"] == "insufficient_quota"
    assert err["reason"] == "monthly_token_budget_exceeded"
