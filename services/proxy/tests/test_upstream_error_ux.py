# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Production must never silently serve mock answers (actionable-error UX).

demo_mode=True keeps the deterministic mock (offline demos, tests).
demo_mode=False with no routable upstream must fail loudly with a message
that tells the caller exactly how to fix it (BYOK headers, provider
prefix, or operator default).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings


def _client(**kwargs) -> TestClient:
    return TestClient(create_app(ProxySettings(**kwargs)))


def test_demo_mode_still_serves_mock():
    client = _client(demo_mode=True)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer demo"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"]


def test_production_without_upstream_fails_actionably():
    client = _client(demo_mode=False)  # no upstream configured, no BYOK header
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer demo"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 502
    detail = str(resp.json())
    # The error must be a recipe, not a shrug:
    assert "x-plynf-upstream-key" in detail
    assert "PLINTH_PROXY_UPSTREAM_BASE_URL" in detail
    assert "gpt-4o" in detail


def test_production_with_byok_header_routes(monkeypatch):
    import plinth_proxy.api as api_mod

    captured: dict = {}

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "id": "chatcmpl-1",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "real"},
                     "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResp()

    monkeypatch.setattr(api_mod.httpx, "AsyncClient", _FakeClient)

    client = _client(demo_mode=False)
    resp = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer demo",
            "x-plynf-upstream": "https://api.openai.com",
            "x-plynf-upstream-key": "sk-byok",
        },
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "real"
    assert captured["headers"]["Authorization"] == "Bearer sk-byok"
