# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.2 LLM client surface (`plinth.llm`).

Coverage outline:

* Provider configuration: ``use_provider`` + ``use_custom_provider``.
* Sync + async ``complete`` / ``stream`` happy paths against the
  MockProvider.
* Retry loop: 429 honours ``retry_after_seconds``, 5xx exponential
  back-off, no retry on non-429 4xx, ``LLMRetryExhausted`` after
  budget expiry.
* Audit recording on success, audit failure swallowed.
* :class:`LLMProviderNotConfigured` raised when nothing configured.
* Token counts agree with :mod:`plinth.tokens`.

All retry tests monkeypatch ``time.sleep`` / ``asyncio.sleep`` so the
suite stays sub-second.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from plinth import (
    LLMError,
    LLMMessage,
    LLMProviderError,
    LLMProviderNotConfigured,
    LLMRateLimited,
    LLMResponse,
    LLMRetryExhausted,
    Plinth,
)
from plinth import tokens as tokens_module
from plinth.llm import LLMClient, _approx_input_tokens
from plinth.llm_providers.mock import MOCK_PRICING, MockProvider


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------


def test_client_llm_is_attached(client: Plinth):
    assert isinstance(client.llm, LLMClient)
    assert client.llm.provider is None


def test_use_provider_mock_returns_provider(client: Plinth):
    provider = client.llm.use_provider("mock", responses=["hello"])
    assert isinstance(provider, MockProvider)
    assert client.llm.provider is provider


def test_use_provider_unknown_raises(client: Plinth):
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        client.llm.use_provider("nope")


def test_use_custom_provider(client: Plinth):
    custom = MockProvider(responses=["x"])
    client.llm.use_custom_provider(custom)
    assert client.llm.provider is custom


def test_complete_without_provider_raises(client: Plinth, monkeypatch):
    # Make sure neither env var is set so auto-detect doesn't kick in.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMProviderNotConfigured):
        client.llm.complete(model="x", messages=[{"role": "user", "content": "hi"}])


def test_auto_detect_anthropic_from_env(client: Plinth, monkeypatch):
    """If only ``ANTHROPIC_API_KEY`` is set, Anthropic auto-configures."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # We don't actually call .complete() (would hit the real API); we
    # just verify ``_ensure_provider`` selects Anthropic.
    provider = client.llm._ensure_provider()  # noqa: SLF001
    assert provider.name == "anthropic"


def test_auto_detect_openai_from_env(client: Plinth, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    provider = client.llm._ensure_provider()  # noqa: SLF001
    assert provider.name == "openai"


def test_auto_detect_anthropic_wins_over_openai(client: Plinth, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    provider = client.llm._ensure_provider()  # noqa: SLF001
    assert provider.name == "anthropic"


# ---------------------------------------------------------------------------
# complete() / stream() with MockProvider
# ---------------------------------------------------------------------------


def test_complete_returns_llmresponse(client: Plinth):
    client.llm.use_provider("mock", responses=["hello world"])
    resp = client.llm.complete(
        model="mock-model",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert isinstance(resp, LLMResponse)
    assert resp.content == "hello world"
    assert resp.provider == "mock"
    assert resp.model == "mock-model"
    assert resp.finish_reason == "stop"
    assert resp.input_tokens > 0
    assert resp.output_tokens > 0


def test_complete_messages_as_llmmessage(client: Plinth):
    client.llm.use_provider("mock", responses=["ok"])
    resp = client.llm.complete(
        model="mock-model",
        messages=[LLMMessage(role="user", content="ping")],
    )
    assert resp.content == "ok"


def test_complete_token_counts_match_tokens_module(client: Plinth):
    client.llm.use_provider("mock", responses=["abcdef"])
    resp = client.llm.complete(
        model="mock-model",
        messages=[{"role": "user", "content": "ping"}],
    )
    expected_input = tokens_module.count("ping")
    expected_output = tokens_module.count("abcdef")
    assert resp.input_tokens == expected_input
    assert resp.output_tokens == expected_output


def test_complete_cost_uses_provider_pricing(client: Plinth):
    client.llm.use_provider("mock", responses=["yo"])
    resp = client.llm.complete(
        model="mock-default",
        messages=[{"role": "user", "content": "abc"}],
    )
    pricing = MOCK_PRICING["mock-default"]
    expected = (
        resp.input_tokens * pricing["input"] + resp.output_tokens * pricing["output"]
    )
    assert resp.cost_usd == pytest.approx(expected)


def test_complete_duration_ms_set(client: Plinth):
    client.llm.use_provider("mock", responses=["x"])
    resp = client.llm.complete(
        model="m", messages=[{"role": "user", "content": "y"}]
    )
    assert resp.duration_ms >= 0


def test_complete_cycles_through_responses(client: Plinth):
    client.llm.use_provider("mock", responses=["a", "b", "c"])
    msgs = [{"role": "user", "content": "go"}]
    out = [
        client.llm.complete(model="m", messages=msgs).content for _ in range(5)
    ]
    assert out == ["a", "b", "c", "a", "b"]


def test_complete_with_max_tokens_and_temperature(client: Plinth):
    """``max_tokens`` and ``temperature`` are accepted (forwarded to provider)."""
    client.llm.use_provider("mock", responses=["ok"])
    resp = client.llm.complete(
        model="m",
        messages=[{"role": "user", "content": "go"}],
        max_tokens=128,
        temperature=0.4,
    )
    assert resp.content == "ok"


def test_stream_yields_chunks(client: Plinth):
    client.llm.use_provider(
        "mock", responses=["abcdefghij"], chunk_size=3
    )
    chunks = list(
        client.llm.stream(
            model="m", messages=[{"role": "user", "content": "go"}]
        )
    )
    text = "".join(c.delta for c in chunks)
    assert text == "abcdefghij"
    # Last chunk carries finish_reason.
    assert chunks[-1].finish_reason == "stop"


def test_stream_empty_response_emits_one_chunk(client: Plinth):
    client.llm.use_provider("mock", responses=[""])
    chunks = list(
        client.llm.stream(
            model="m", messages=[{"role": "user", "content": "go"}]
        )
    )
    assert len(chunks) == 1
    assert chunks[0].finish_reason == "stop"


# ---------------------------------------------------------------------------
# Async surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acomplete_returns_response(client: Plinth):
    client.llm.use_provider("mock", responses=["async-ok"])
    resp = await client.llm.acomplete(
        model="m", messages=[{"role": "user", "content": "go"}]
    )
    assert resp.content == "async-ok"
    assert resp.duration_ms >= 0


@pytest.mark.asyncio
async def test_astream_yields_chunks(client: Plinth):
    client.llm.use_provider(
        "mock", responses=["123456"], chunk_size=2
    )
    chunks = []
    async for chunk in client.llm.astream(
        model="m", messages=[{"role": "user", "content": "go"}]
    ):
        chunks.append(chunk)
    text = "".join(c.delta for c in chunks)
    assert text == "123456"
    assert chunks[-1].finish_reason == "stop"


# ---------------------------------------------------------------------------
# Retry loop
# ---------------------------------------------------------------------------


class _FlakyProvider:
    """Counts calls and emits configurable errors before succeeding."""

    name = "flaky"

    def __init__(self, errors: list[Exception], success: LLMResponse) -> None:
        self._errors = list(errors)
        self._success = success
        self.calls = 0

    def complete(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._success

    def stream(self, **kwargs):  # type: ignore[no-untyped-def]
        return iter([])

    async def acomplete(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._success

    async def astream(self, **kwargs):  # type: ignore[no-untyped-def]
        async def _empty():
            if False:  # pragma: no cover
                yield None

        return _empty()

    def estimate_cost_usd(self, model, input_tokens, output_tokens):  # type: ignore[no-untyped-def]
        return 0.0


def _ok() -> LLMResponse:
    return LLMResponse(
        content="ok",
        model="m",
        finish_reason="stop",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        duration_ms=0,
        provider="flaky",
        raw={},
    )


def test_retry_on_429_with_retry_after(client: Plinth):
    err = LLMRateLimited(
        "rate limited",
        retry_after_seconds=2.5,
        status_code=429,
        provider="flaky",
    )
    p = _FlakyProvider([err, err], _ok())
    client.llm.use_custom_provider(p)
    sleeps: list[float] = []
    with patch("plinth.llm.time.sleep", side_effect=sleeps.append):
        resp = client.llm.complete(
            model="m", messages=[{"role": "user", "content": "go"}]
        )
    assert resp.content == "ok"
    assert p.calls == 3
    # Both retry sleeps used the server-provided retry_after.
    assert sleeps == [2.5, 2.5]


def test_retry_on_5xx_uses_backoff(client: Plinth):
    err = LLMProviderError(
        "internal", status_code=503, provider="flaky"
    )
    p = _FlakyProvider([err, err], _ok())
    client.llm.use_custom_provider(p)
    client.llm.configure_retries(retries=3, backoff_seconds=1.0)
    sleeps: list[float] = []
    with patch("plinth.llm.time.sleep", side_effect=sleeps.append):
        resp = client.llm.complete(
            model="m", messages=[{"role": "user", "content": "go"}]
        )
    assert resp.content == "ok"
    # 1.0 then 2.0 (exponential).
    assert sleeps == [1.0, 2.0]


def test_no_retry_on_4xx_other_than_429(client: Plinth):
    err = LLMProviderError("bad request", status_code=400, provider="flaky")
    p = _FlakyProvider([err], _ok())
    client.llm.use_custom_provider(p)
    with patch("plinth.llm.time.sleep") as sl, pytest.raises(LLMProviderError):
        client.llm.complete(
            model="m", messages=[{"role": "user", "content": "go"}]
        )
    sl.assert_not_called()
    assert p.calls == 1


def test_retry_exhausted_after_max_attempts(client: Plinth):
    errs = [
        LLMRateLimited(
            "rl", retry_after_seconds=0.1, status_code=429, provider="flaky"
        )
        for _ in range(10)
    ]
    p = _FlakyProvider(errs, _ok())
    client.llm.use_custom_provider(p)
    client.llm.configure_retries(retries=2, backoff_seconds=0.1)
    with patch("plinth.llm.time.sleep"), pytest.raises(LLMRetryExhausted) as info:
        client.llm.complete(
            model="m", messages=[{"role": "user", "content": "go"}]
        )
    # 1 initial + 2 retries.
    assert p.calls == 3
    assert isinstance(info.value.__cause__, LLMRateLimited)


def test_retry_on_5xx_falls_back_when_exhausted(client: Plinth):
    err = LLMProviderError("up", status_code=502, provider="flaky")
    errs = [err, err, err, err]
    p = _FlakyProvider(errs, _ok())
    client.llm.use_custom_provider(p)
    client.llm.configure_retries(retries=2, backoff_seconds=0.0)
    # The retry loop re-raises the underlying ``LLMProviderError`` on
    # the final attempt rather than wrapping it in
    # ``LLMRetryExhausted``, because the last attempt itself raised the
    # provider error and the loop short-circuits before the wrapper.
    with patch("plinth.llm.time.sleep"), pytest.raises(LLMProviderError):
        client.llm.complete(
            model="m", messages=[{"role": "user", "content": "go"}]
        )


def test_retries_configurable_to_zero(client: Plinth):
    err = LLMRateLimited("rl", retry_after_seconds=0.1, status_code=429)
    p = _FlakyProvider([err], _ok())
    client.llm.use_custom_provider(p)
    client.llm.configure_retries(retries=0)
    with patch("plinth.llm.time.sleep"), pytest.raises(LLMRetryExhausted):
        client.llm.complete(
            model="m", messages=[{"role": "user", "content": "go"}]
        )
    assert p.calls == 1


@pytest.mark.asyncio
async def test_async_retry_on_429(client: Plinth):
    err = LLMRateLimited("rl", retry_after_seconds=0.5)
    p = _FlakyProvider([err], _ok())
    client.llm.use_custom_provider(p)
    client.llm.configure_retries(retries=1, backoff_seconds=0.0)
    sleeps: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)

    with patch("plinth.llm.asyncio.sleep", side_effect=fake_sleep):
        resp = await client.llm.acomplete(
            model="m", messages=[{"role": "user", "content": "go"}]
        )
    assert resp.content == "ok"
    assert sleeps == [0.5]


# ---------------------------------------------------------------------------
# Audit recording
# ---------------------------------------------------------------------------


def test_audit_recorded_on_success(
    client: Plinth, gateway_mock: respx.MockRouter
):
    client.llm.use_provider("mock", responses=["hi"])
    captured: dict[str, Any] = {}

    def handler(request):
        captured["body"] = request.read()
        return httpx.Response(201, json={"audit_id": "evt_LLM_TEST"})

    gateway_mock.post("/v1/audit/record-llm").mock(side_effect=handler)
    resp = client.llm.complete(
        model="m",
        messages=[{"role": "user", "content": "go"}],
        workspace_id="ws_X",
        agent_id="agent_a",
    )
    assert resp.audit_id == "evt_LLM_TEST"
    body = captured["body"].decode()
    assert "llm.mock" in body
    assert "ws_X" in body
    assert "agent_a" in body


def test_audit_failure_does_not_break_call(
    client: Plinth, gateway_mock: respx.MockRouter
):
    """Audit POST 5xx must not cause complete() to raise."""
    client.llm.use_provider("mock", responses=["hi"])
    gateway_mock.post("/v1/audit/record-llm").mock(
        return_value=httpx.Response(500, json={"error": {"code": "BOOM"}})
    )
    resp = client.llm.complete(
        model="m", messages=[{"role": "user", "content": "go"}]
    )
    assert resp.content == "hi"
    # audit_id stays None when the audit failed.
    assert resp.audit_id is None


def test_audit_recorded_on_stream(
    client: Plinth, gateway_mock: respx.MockRouter
):
    client.llm.use_provider("mock", responses=["abcdef"], chunk_size=2)
    captured: list[bytes] = []
    gateway_mock.post("/v1/audit/record-llm").mock(
        side_effect=lambda req: (
            captured.append(req.read())
            or httpx.Response(201, json={"audit_id": "evt_STREAM"})
        )
    )
    chunks = list(
        client.llm.stream(
            model="m",
            messages=[{"role": "user", "content": "go"}],
            workspace_id="ws_S",
        )
    )
    assert "".join(c.delta for c in chunks) == "abcdef"
    assert captured, "audit endpoint was called"
    body = captured[0].decode()
    assert "llm.mock" in body
    assert "ws_S" in body


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_approx_input_tokens_handles_dicts_and_messages():
    n_dict = _approx_input_tokens([{"role": "user", "content": "hello"}])
    n_msg = _approx_input_tokens([LLMMessage(role="user", content="hello")])
    assert n_dict == n_msg
    assert n_dict > 0


def test_llm_error_class_hierarchy():
    """All LLM exceptions derive from ``LLMError`` for catch-all use."""
    assert issubclass(LLMRateLimited, LLMProviderError)
    assert issubclass(LLMProviderError, LLMError)
    assert issubclass(LLMRetryExhausted, LLMError)
    assert issubclass(LLMProviderNotConfigured, LLMError)
