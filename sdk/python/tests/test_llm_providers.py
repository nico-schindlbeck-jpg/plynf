# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.2 LLM provider adapters.

The Anthropic + OpenAI adapters are exercised via lightweight stand-in
clients (``unittest.mock`` shapes) so the SDK never touches the real
vendor APIs. The MockProvider gets its own focused tests because it's
the engine the rest of the test suite (and example 06) runs on.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plinth import (
    LLMMessage,
    LLMProviderError,
    LLMRateLimited,
)
from plinth.llm_providers import build_provider
from plinth.llm_providers.anthropic import (
    ANTHROPIC_PRICING,
    AnthropicProvider,
)
from plinth.llm_providers.mock import MockProvider
from plinth.llm_providers.openai import OPENAI_PRICING, OpenAIProvider


# ---------------------------------------------------------------------------
# build_provider dispatch
# ---------------------------------------------------------------------------


def test_build_provider_mock():
    provider = build_provider("mock", responses=["x"])
    assert isinstance(provider, MockProvider)
    assert provider.name == "mock"


def test_build_provider_anthropic_returns_provider():
    provider = build_provider("anthropic", api_key="sk-ant-test")
    assert isinstance(provider, AnthropicProvider)
    assert provider.name == "anthropic"


def test_build_provider_openai_returns_provider():
    provider = build_provider("openai", api_key="sk-test")
    assert isinstance(provider, OpenAIProvider)
    assert provider.name == "openai"


def test_build_provider_unknown():
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        build_provider("xyzzy")


# ---------------------------------------------------------------------------
# MockProvider behaviour
# ---------------------------------------------------------------------------


def test_mock_complete_default_response():
    p = MockProvider()
    resp = p.complete(messages=[{"role": "user", "content": "x"}])
    assert resp.content == "mock response"
    assert resp.provider == "mock"
    assert resp.model == "mock-model"


def test_mock_complete_with_dict_responses():
    p = MockProvider(
        responses=[
            {"role": "assistant", "content": "from-dict"},
            "from-string",
        ]
    )
    a = p.complete(messages=[{"role": "user", "content": "x"}])
    b = p.complete(messages=[{"role": "user", "content": "x"}])
    assert a.content == "from-dict"
    assert b.content == "from-string"


def test_mock_stream_chunks_are_chunk_size():
    p = MockProvider(responses=["abcdefghij"], chunk_size=4)
    chunks = list(p.stream(messages=[{"role": "user", "content": "x"}]))
    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["abcd", "efgh", "ij"]
    assert chunks[-1].finish_reason == "stop"


def test_mock_estimate_cost_known_model():
    p = MockProvider()
    cost = p.estimate_cost_usd("mock-default", 1000, 1000)
    # 1000 * 1e-6 + 1000 * 2e-6 = 0.003
    assert cost == pytest.approx(0.003)


def test_mock_estimate_cost_unknown_model_falls_back():
    p = MockProvider()
    a = p.estimate_cost_usd("mock-default", 100, 100)
    b = p.estimate_cost_usd("nonexistent-model", 100, 100)
    assert a == b  # falls back to mock-default


@pytest.mark.asyncio
async def test_mock_acomplete_works():
    p = MockProvider(responses=["async"])
    resp = await p.acomplete(messages=[{"role": "user", "content": "x"}])
    assert resp.content == "async"


@pytest.mark.asyncio
async def test_mock_astream_works():
    p = MockProvider(responses=["abcdef"], chunk_size=2)
    chunks = []
    async for c in p.astream(messages=[{"role": "user", "content": "x"}]):
        chunks.append(c)
    assert "".join(c.delta for c in chunks) == "abcdef"
    assert chunks[-1].finish_reason == "stop"


# ---------------------------------------------------------------------------
# Cost calculation: every published price tier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model,pricing", list(ANTHROPIC_PRICING.items()))
def test_anthropic_pricing_all_models(model: str, pricing: dict[str, float]):
    """Ensure every entry in :data:`ANTHROPIC_PRICING` is wired correctly."""
    provider = AnthropicProvider(api_key="sk-ant-test")
    cost = provider.estimate_cost_usd(model, 1_000_000, 0)
    # 1M input tokens at the listed rate.
    assert cost == pytest.approx(pricing["input"] * 1_000_000)
    cost_output = provider.estimate_cost_usd(model, 0, 1_000_000)
    assert cost_output == pytest.approx(pricing["output"] * 1_000_000)


def test_anthropic_pricing_combined():
    provider = AnthropicProvider(api_key="sk-ant-test")
    cost = provider.estimate_cost_usd("claude-sonnet-4-5", 1000, 500)
    expected = 1000 * (3.0 / 1_000_000) + 500 * (15.0 / 1_000_000)
    assert cost == pytest.approx(expected)


def test_anthropic_pricing_unknown_falls_back_to_sonnet():
    provider = AnthropicProvider(api_key="sk-ant-test")
    a = provider.estimate_cost_usd("claude-sonnet-4-5", 100, 100)
    b = provider.estimate_cost_usd("claude-future-model", 100, 100)
    assert a == b


@pytest.mark.parametrize("model,pricing", list(OPENAI_PRICING.items()))
def test_openai_pricing_all_models(model: str, pricing: dict[str, float]):
    provider = OpenAIProvider(api_key="sk-test")
    cost = provider.estimate_cost_usd(model, 1_000_000, 0)
    assert cost == pytest.approx(pricing["input"] * 1_000_000)
    cost_output = provider.estimate_cost_usd(model, 0, 1_000_000)
    assert cost_output == pytest.approx(pricing["output"] * 1_000_000)


def test_openai_pricing_unknown_falls_back():
    provider = OpenAIProvider(api_key="sk-test")
    a = provider.estimate_cost_usd("gpt-5-mini", 100, 100)
    b = provider.estimate_cost_usd("gpt-9999", 100, 100)
    assert a == b


# ---------------------------------------------------------------------------
# Anthropic message translation
# ---------------------------------------------------------------------------


def test_anthropic_split_system_extracts_system_messages():
    messages = [
        LLMMessage(role="system", content="You are helpful."),
        LLMMessage(role="user", content="hi"),
    ]
    system, chat = AnthropicProvider._split_system(messages)
    assert system == "You are helpful."
    assert chat == [{"role": "user", "content": "hi"}]


def test_anthropic_split_system_concatenates_multiple_systems():
    messages = [
        {"role": "system", "content": "First."},
        {"role": "system", "content": "Second."},
        {"role": "user", "content": "hi"},
    ]
    system, chat = AnthropicProvider._split_system(messages)
    assert system == "First.\n\nSecond."
    assert len(chat) == 1


def test_anthropic_split_system_unknown_role_collapses_to_user():
    messages = [{"role": "tool", "content": "result"}]
    system, chat = AnthropicProvider._split_system(messages)
    assert system is None
    assert chat == [{"role": "user", "content": "result"}]


def test_anthropic_extract_content_from_text_blocks():
    text_block = SimpleNamespace(text="hello")
    msg = SimpleNamespace(content=[text_block])
    out = AnthropicProvider._extract_content(msg)
    assert out == "hello"


def test_anthropic_extract_usage_from_response():
    msg = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=10, output_tokens=20)
    )
    in_tok, out_tok = AnthropicProvider._extract_usage(msg)
    assert (in_tok, out_tok) == (10, 20)


def test_anthropic_extract_usage_missing_returns_zero():
    msg = SimpleNamespace()
    in_tok, out_tok = AnthropicProvider._extract_usage(msg)
    assert (in_tok, out_tok) == (0, 0)


# ---------------------------------------------------------------------------
# Anthropic complete() with a stubbed client
# ---------------------------------------------------------------------------


class _StubAnthropicMessages:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


class _StubAnthropicClient:
    def __init__(self, response: Any) -> None:
        self.messages = _StubAnthropicMessages(response)


def _build_anthropic_message(text: str = "hi", input_tokens=5, output_tokens=7):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens
        ),
        stop_reason="end_turn",
        model="claude-sonnet-4-5",
        model_dump=lambda: {"content": [{"text": text}], "stop_reason": "end_turn"},
    )


def test_anthropic_complete_translates_response():
    provider = AnthropicProvider(api_key="sk-ant-test")
    provider._client = _StubAnthropicClient(_build_anthropic_message("output text"))
    resp = provider.complete(
        model="claude-sonnet-4-5",
        messages=[
            LLMMessage(role="system", content="be brief"),
            LLMMessage(role="user", content="hi"),
        ],
        max_tokens=64,
    )
    assert resp.content == "output text"
    assert resp.input_tokens == 5
    assert resp.output_tokens == 7
    assert resp.finish_reason == "end_turn"
    assert resp.provider == "anthropic"
    assert resp.cost_usd == pytest.approx(
        5 * (3.0 / 1_000_000) + 7 * (15.0 / 1_000_000)
    )
    sent = provider._client.messages.last_kwargs
    assert sent["system"] == "be brief"
    assert sent["model"] == "claude-sonnet-4-5"


def _make_httpx_response(*, status_code: int, headers=None) -> Any:
    """Build an ``httpx.Response`` suitable for vendor SDK exceptions.

    Both ``anthropic`` and ``openai`` instantiate their typed errors
    with a real ``httpx.Response`` (they reach for ``response.request``
    in their constructors). A plain ``SimpleNamespace`` won't do; we
    need the genuine article.
    """
    import httpx

    request = httpx.Request("GET", "https://example.test/v1/messages")
    return httpx.Response(
        status_code,
        headers=headers or {},
        request=request,
    )


def test_anthropic_complete_wraps_status_error():
    """A non-rate-limit ``APIStatusError`` becomes ``LLMProviderError``."""
    import anthropic

    err = anthropic.APIStatusError(
        "bad",
        response=_make_httpx_response(status_code=400),
        body=None,
    )
    provider = AnthropicProvider(api_key="sk-ant-test")
    provider._client = _StubAnthropicClient(err)
    with pytest.raises(LLMProviderError):
        provider.complete(
            model="claude-sonnet-4-5",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=10,
        )


def test_anthropic_complete_wraps_rate_limit():
    """A 429 from the SDK comes out as :class:`LLMRateLimited`."""
    import anthropic

    err = anthropic.RateLimitError(
        "slow down",
        response=_make_httpx_response(
            status_code=429, headers={"retry-after": "7"}
        ),
        body=None,
    )
    provider = AnthropicProvider(api_key="sk-ant-test")
    provider._client = _StubAnthropicClient(err)
    with pytest.raises(LLMRateLimited) as info:
        provider.complete(
            model="claude-sonnet-4-5",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=10,
        )
    assert info.value.retry_after_seconds == 7.0
    assert info.value.status_code == 429


# ---------------------------------------------------------------------------
# OpenAI message translation + complete()
# ---------------------------------------------------------------------------


def test_openai_to_chat_keeps_roles():
    out = OpenAIProvider._to_chat(
        [
            LLMMessage(role="system", content="be helpful"),
            LLMMessage(role="user", content="hi"),
        ]
    )
    assert out == [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
    ]


def test_openai_to_chat_drops_none_keys_in_dicts():
    out = OpenAIProvider._to_chat(
        [{"role": "user", "content": "hi", "name": None}]
    )
    assert out == [{"role": "user", "content": "hi"}]


def test_openai_extract_content_handles_none():
    completion = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None),
                finish_reason="tool_calls",
            )
        ]
    )
    content, fr = OpenAIProvider._extract_content(completion)
    assert content == ""
    assert fr == "tool_calls"


class _StubOpenAICompletions:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


class _StubOpenAIChat:
    def __init__(self, response: Any) -> None:
        self.completions = _StubOpenAICompletions(response)


class _StubOpenAIClient:
    def __init__(self, response: Any) -> None:
        self.chat = _StubOpenAIChat(response)


def _build_openai_completion(content="ok", in_tok=3, out_tok=4, finish="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish,
            )
        ],
        usage=SimpleNamespace(prompt_tokens=in_tok, completion_tokens=out_tok),
        model="gpt-5",
        model_dump=lambda: {"content": content},
    )


def test_openai_complete_translates_response():
    provider = OpenAIProvider(api_key="sk-test")
    provider._client = _StubOpenAIClient(_build_openai_completion("hello"))
    resp = provider.complete(
        model="gpt-5",
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=64,
        temperature=0.2,
    )
    assert resp.content == "hello"
    assert resp.input_tokens == 3
    assert resp.output_tokens == 4
    assert resp.provider == "openai"
    sent = provider._client.chat.completions.last_kwargs
    assert sent["model"] == "gpt-5"
    assert sent["temperature"] == 0.2
    assert sent["max_tokens"] == 64


def test_openai_complete_rate_limit_maps_to_rate_limited():
    import openai

    err = openai.RateLimitError(
        "slow",
        response=_make_httpx_response(
            status_code=429, headers={"retry-after": "3"}
        ),
        body=None,
    )
    provider = OpenAIProvider(api_key="sk-test")
    provider._client = _StubOpenAIClient(err)
    with pytest.raises(LLMRateLimited) as info:
        provider.complete(
            model="gpt-5",
            messages=[{"role": "user", "content": "x"}],
        )
    assert info.value.retry_after_seconds == 3.0


def test_openai_complete_status_error_maps_to_provider_error():
    import openai

    err = openai.APIStatusError(
        "boom",
        response=_make_httpx_response(status_code=503),
        body=None,
    )
    provider = OpenAIProvider(api_key="sk-test")
    provider._client = _StubOpenAIClient(err)
    with pytest.raises(LLMProviderError):
        provider.complete(
            model="gpt-5",
            messages=[{"role": "user", "content": "x"}],
        )


# ---------------------------------------------------------------------------
# OpenAI streaming
# ---------------------------------------------------------------------------


def test_openai_stream_yields_deltas():
    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="hel"),
                    finish_reason=None,
                )
            ]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="lo"),
                    finish_reason=None,
                )
            ]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None),
                    finish_reason="stop",
                )
            ]
        ),
    ]
    provider = OpenAIProvider(api_key="sk-test")
    provider._client = _StubOpenAIClient(iter(chunks))
    out = list(
        provider.stream(
            model="gpt-5",
            messages=[{"role": "user", "content": "x"}],
        )
    )
    deltas = [c.delta for c in out if c.delta]
    assert deltas == ["hel", "lo"]
    assert out[-1].finish_reason == "stop"
