# SPDX-License-Identifier: Apache-2.0
"""Tests for OpenAI drop-in completeness: /v1/models, /v1/models/{model},
/v1/embeddings — both demo-mode mocks and upstream forwarding."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from plinth_proxy import api
from plinth_proxy.api import create_app
from plinth_proxy.mock_llm import _deterministic_vector, mock_embeddings, mock_models
from plinth_proxy.settings import ProxySettings

# ---------------------------------------------------------------------------
# Fake upstream httpx client (no network)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, captured: dict, payload):
        self._captured = captured
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self._captured.update(method="GET", url=url, headers=headers, json=None)
        return _FakeResp(self._payload)

    async def request(self, method, url, json=None, headers=None):
        self._captured.update(method=method, url=url, headers=headers, json=json)
        return _FakeResp(self._payload)


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


# ---------------------------------------------------------------------------
# Unit: mock helpers
# ---------------------------------------------------------------------------


def test_mock_models_shape():
    out = mock_models()
    assert out["object"] == "list"
    ids = {m["id"] for m in out["data"]}
    assert "gpt-4o" in ids
    assert all(m["object"] == "model" and m["owned_by"] == "plynf-proxy" for m in out["data"])


def test_deterministic_vector_is_stable_and_normalised():
    v1 = _deterministic_vector("hello world", 16)
    v2 = _deterministic_vector("hello world", 16)
    assert v1 == v2  # deterministic
    assert len(v1) == 16
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-9  # L2-normalised
    # Different text → different vector.
    assert _deterministic_vector("different", 16) != v1


def test_mock_embeddings_list_input_and_dimensions():
    out = mock_embeddings(
        {"input": ["a", "b", "c"], "dimensions": 8, "model": "text-embedding-3-small"}
    )
    assert out["object"] == "list"
    assert [d["index"] for d in out["data"]] == [0, 1, 2]
    assert all(len(d["embedding"]) == 8 for d in out["data"])
    assert out["model"] == "text-embedding-3-small"
    assert out["usage"]["total_tokens"] >= 1


# ---------------------------------------------------------------------------
# Demo-mode endpoints
# ---------------------------------------------------------------------------


def test_get_models_demo(demo_client):
    r = demo_client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert any(m["id"] == "gpt-4o" for m in body["data"])


def test_retrieve_model_demo(demo_client):
    r = demo_client.get("/v1/models/gpt-4o-mini")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "gpt-4o-mini"
    assert body["object"] == "model"


def test_embeddings_demo_string_input(demo_client):
    r = demo_client.post(
        "/v1/embeddings", json={"input": "shape this", "model": "text-embedding-3-small"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert len(body["data"]) == 1
    assert len(body["data"][0]["embedding"]) == 16  # default dim
    assert body["data"][0]["index"] == 0


def test_embeddings_demo_is_deterministic(demo_client):
    payload = {"input": "same text", "model": "text-embedding-3-small"}
    a = demo_client.post("/v1/embeddings", json=payload).json()
    b = demo_client.post("/v1/embeddings", json=payload).json()
    assert a["data"][0]["embedding"] == b["data"][0]["embedding"]


# ---------------------------------------------------------------------------
# Upstream forwarding (real-upstream mode)
# ---------------------------------------------------------------------------


def _upstream_client(monkeypatch, captured, payload):
    monkeypatch.setattr(
        api.httpx, "AsyncClient", lambda *a, **k: _FakeClient(captured, payload)
    )
    settings = ProxySettings(
        demo_mode=False,
        upstream_base_url="https://up.test",
        upstream_api_key="sk-upstream",
    )
    return TestClient(create_app(settings))


def test_get_models_forwards_to_upstream(monkeypatch):
    captured: dict = {}
    upstream_payload = {"object": "list", "data": [{"id": "gpt-4o", "object": "model"}]}
    client = _upstream_client(monkeypatch, captured, upstream_payload)

    r = client.get("/v1/models")
    assert r.status_code == 200
    assert r.json() == upstream_payload
    assert captured["method"] == "GET"
    assert captured["url"] == "https://up.test/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-upstream"


def test_embeddings_forwards_to_upstream(monkeypatch):
    captured: dict = {}
    upstream_payload = {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        "model": "text-embedding-3-large",
    }
    client = _upstream_client(monkeypatch, captured, upstream_payload)

    req = {"input": "hello", "model": "text-embedding-3-large"}
    r = client.post("/v1/embeddings", json=req)
    assert r.status_code == 200
    assert r.json() == upstream_payload
    assert captured["method"] == "POST"
    assert captured["url"] == "https://up.test/v1/embeddings"
    assert captured["json"] == req
    assert captured["headers"]["Authorization"] == "Bearer sk-upstream"
