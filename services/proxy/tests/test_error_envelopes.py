# SPDX-License-Identifier: Apache-2.0
"""Dialect-aware HTTP error envelopes.

Plynf fronts many vendor APIs whose SDKs each parse *errors* in their own
shape. A client that points its base URL at Plynf must see *its own* error
shape on a 401/402/404 — otherwise its error handling breaks, defeating the
same "no code change" promise the success-path translators keep. These tests
pin ``error_body``'s per-dialect output and prove the FastAPI exception handler
reshapes errors end-to-end through every front door.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.error_envelopes import _dialect_for_path, _split_detail, error_body
from plinth_proxy.settings import ProxySettings

# ---------------------------------------------------------------------------
# _dialect_for_path — every front door classifies to its vendor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/v1/chat/completions", "openai"),
        ("/openai/deployments/gpt-4o/chat/completions", "openai"),
        ("/v1/responses", "openai"),
        ("/v1/embeddings", "openai"),
        ("/v1/messages", "anthropic"),
        ("/v1/projects/p/locations/l/publishers/anthropic/models/m:rawPredict", "anthropic"),
        ("/v1beta/models/gemini-1.5-pro:generateContent", "gemini"),
        ("/v1/projects/p/locations/l/publishers/google/models/m:generateContent", "gemini"),
        ("/v2/chat", "cohere"),
        ("/model/anthropic.claude-3-5-sonnet/converse", "bedrock"),
    ],
)
def test_dialect_for_path(path, expected):
    assert _dialect_for_path(path) == expected


# ---------------------------------------------------------------------------
# _split_detail — string vs structured detail
# ---------------------------------------------------------------------------


def test_split_detail_string_is_message_with_no_extra():
    assert _split_detail("missing api key") == ("missing api key", {})


def test_split_detail_dict_prefers_message_then_reason_then_error():
    assert _split_detail({"message": "m", "x": 1}) == ("m", {"x": 1})
    # No "message" → fall back to "reason"; reason stays in extra too.
    assert _split_detail({"reason": "r", "x": 1}) == ("r", {"reason": "r", "x": 1})
    # No message/reason → fall back to "error".
    assert _split_detail({"error": "e"}) == ("e", {"error": "e"})
    # Empty-ish dict → the literal "error" sentinel.
    assert _split_detail({}) == ("error", {})


# ---------------------------------------------------------------------------
# error_body — OpenAI (default) dialect
# ---------------------------------------------------------------------------


def test_openai_envelope_string_detail():
    assert error_body("/v1/chat/completions", 401, "missing api key") == {
        "error": {
            "message": "missing api key",
            "type": "invalid_request_error",
            "param": None,
            "code": None,
        }
    }


def test_openai_envelope_5xx_is_api_error():
    body = error_body("/v1/chat/completions", 502, "upstream error")
    assert body["error"]["type"] == "api_error"


def test_openai_envelope_preserves_tier_limit_fields():
    # The structured 402 payload must survive inside the envelope so the client
    # keeps the upgrade hint; detail["error"] is promoted to the OpenAI ``code``.
    detail = {
        "error": "tier_limit_exceeded",
        "reason": "monthly_token_budget_exceeded",
        "tier": "free",
        "upgrade_hint": "Upgrade to Pro for 10x the budget.",
    }
    err = error_body("/v1/chat/completions", 402, detail)["error"]
    assert err["type"] == "insufficient_quota"
    assert err["code"] == "tier_limit_exceeded"
    assert err["message"] == "monthly_token_budget_exceeded"
    assert err["param"] is None
    assert err["tier"] == "free"
    assert err["upgrade_hint"].startswith("Upgrade to Pro")


# ---------------------------------------------------------------------------
# error_body — Anthropic dialect
# ---------------------------------------------------------------------------


def test_anthropic_envelope_401_is_authentication_error():
    body = error_body("/v1/messages", 401, "missing api key")
    assert body["type"] == "error"
    assert body["error"] == {"type": "authentication_error", "message": "missing api key"}


def test_anthropic_envelope_402_maps_to_permission_error():
    body = error_body("/v1/messages", 402, "nope")
    assert body["error"]["type"] == "permission_error"


def test_anthropic_envelope_5xx_is_api_error():
    body = error_body("/v1/messages", 503, "down")
    assert body["error"]["type"] == "api_error"


# ---------------------------------------------------------------------------
# error_body — Gemini / Google dialect
# ---------------------------------------------------------------------------


def test_gemini_envelope_known_status():
    body = error_body("/v1beta/models/gemini-1.5-pro:generateContent", 404, "no model")
    assert body["error"] == {"code": 404, "message": "no model", "status": "NOT_FOUND"}


def test_gemini_envelope_unknown_status_falls_back():
    body = error_body("/v1beta/models/m:generateContent", 418, "teapot")
    assert body["error"]["code"] == 418
    assert body["error"]["status"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# error_body — Cohere / Bedrock flat ``{"message": ...}`` dialect
# ---------------------------------------------------------------------------


def test_cohere_envelope_is_flat_message():
    assert error_body("/v2/chat", 401, "missing api key") == {"message": "missing api key"}


def test_bedrock_envelope_is_flat_message():
    assert error_body("/model/anthropic.claude/converse", 404, "unknown tool: x") == {
        "message": "unknown tool: x"
    }


def test_bedrock_envelope_merges_structured_extra():
    body = error_body(
        "/model/m/converse",
        402,
        {"reason": "monthly_token_budget_exceeded", "tier": "free"},
    )
    assert body["message"] == "monthly_token_budget_exceeded"
    assert body["tier"] == "free"
    assert body["reason"] == "monthly_token_budget_exceeded"


# ---------------------------------------------------------------------------
# End-to-end: the handler reshapes errors through the real app, per front door
# ---------------------------------------------------------------------------


@pytest.fixture
def keyed_client():
    # api_keys configured → an unauthenticated request raises 401, exercising
    # the exception handler through the real app and each front door's path.
    return TestClient(create_app(ProxySettings(demo_mode=True, api_keys="t:key:free")))


def test_openai_401_envelope_e2e(keyed_client):
    r = keyed_client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "invalid_request_error"
    assert r.json()["error"]["message"] == "missing api key"


def test_anthropic_401_envelope_e2e(keyed_client):
    r = keyed_client.post("/v1/messages", json={"messages": []})
    assert r.status_code == 401
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"


def test_gemini_401_envelope_e2e(keyed_client):
    r = keyed_client.post(
        "/v1beta/models/gemini-1.5-pro:generateContent", json={"contents": []}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == 401
    assert r.json()["error"]["status"] == "UNAUTHENTICATED"


def test_cohere_401_envelope_e2e(keyed_client):
    r = keyed_client.post("/v2/chat", json={"messages": []})
    assert r.status_code == 401
    assert r.json() == {"message": "missing api key"}


def test_bedrock_401_envelope_e2e(keyed_client):
    r = keyed_client.post("/model/anthropic.claude/converse", json={"messages": []})
    assert r.status_code == 401
    assert r.json() == {"message": "missing api key"}


def test_unknown_tool_404_uses_openai_envelope_e2e():
    # /v1/tools/... is the default (OpenAI) dialect; the webhook 404s on an
    # unregistered tool, and that detail string is reshaped into the envelope.
    client = TestClient(create_app(ProxySettings(demo_mode=True)))
    r = client.post("/v1/tools/does_not_exist/invoke", json={"arguments": {}})
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["type"] == "invalid_request_error"
    assert "unknown tool" in err["message"]


def test_unmatched_route_404_is_reshaped_e2e():
    # A framework-raised 404 (no route matches) must also be reshaped — proof
    # the handler is registered on the Starlette base class, not just our raises.
    client = TestClient(create_app(ProxySettings(demo_mode=True)))
    r = client.get("/this/route/does/not/exist")
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "invalid_request_error"
    assert r.json()["error"]["message"] == "Not Found"
