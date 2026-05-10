# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""OpenAI provider — wraps the official ``openai`` SDK.

Lives behind the optional ``[openai]`` extra. Models the chat-completions
streaming endpoint; the Responses API is intentionally out of scope for
the v1.2 LLM layer. Pricing tracks the gpt-5 generation.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

from ..exceptions import LLMProviderError, LLMRateLimited
from ..models import LLMMessage, LLMResponse, LLMStreamChunk

import openai  # type: ignore[import-untyped]  # noqa: E402


#: Per-token pricing in USD. Numbers are USD/token (not USD/Mtok), so
#: cost = tokens * rate.
#:
#: Source: openai.com/api/pricing (gpt-5 generation).
OPENAI_PRICING: dict[str, dict[str, float]] = {
    "gpt-5": {
        "input": 1.25 / 1_000_000,
        "output": 10.00 / 1_000_000,
    },
    "gpt-5-mini": {
        "input": 0.25 / 1_000_000,
        "output": 2.00 / 1_000_000,
    },
    "gpt-5-nano": {
        "input": 0.05 / 1_000_000,
        "output": 0.40 / 1_000_000,
    },
}

#: Fallback pricing applied when the model isn't in :data:`OPENAI_PRICING`.
_FALLBACK_PRICING: dict[str, float] = OPENAI_PRICING["gpt-5-mini"]


class OpenAIProvider:
    """Adapter over ``openai.OpenAI`` — chat completions surface.

    Args:
        api_key: API key. Defaults to ``OPENAI_API_KEY`` from the
            environment.
        base_url: Override the API base URL (rare; mostly for proxies
            or compatible third-party endpoints).
        client_options: Extra kwargs forwarded to the underlying
            constructor — e.g. for tests injecting a custom HTTP client.
    """

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client_options: dict[str, Any] | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        kwargs: dict[str, Any] = dict(client_options or {})
        if resolved_key:
            kwargs.setdefault("api_key", resolved_key)
        if base_url:
            kwargs.setdefault("base_url", base_url)
        self._client = openai.OpenAI(**kwargs)
        self._async_client: openai.AsyncOpenAI | None = None
        self._async_kwargs = kwargs

    def _get_async(self) -> openai.AsyncOpenAI:
        if self._async_client is None:
            self._async_client = openai.AsyncOpenAI(**self._async_kwargs)
        return self._async_client

    # ------------------------------------------------------------------
    # Message translation
    # ------------------------------------------------------------------

    @staticmethod
    def _to_chat(
        messages: list[LLMMessage] | list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Normalise messages into the OpenAI chat schema."""
        out: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, LLMMessage):
                entry: dict[str, Any] = {
                    "role": msg.role,
                    "content": msg.content,
                }
                if msg.name:
                    entry["name"] = msg.name
                if msg.tool_call_id:
                    entry["tool_call_id"] = msg.tool_call_id
                out.append(entry)
            elif isinstance(msg, dict):
                # Pass dicts through; OpenAI's API will validate. Drop
                # ``None`` keys so the request body stays tidy.
                out.append({k: v for k, v in msg.items() if v is not None})
        return out

    @staticmethod
    def _extract_content(completion: Any) -> tuple[str, str]:
        """Return ``(content, finish_reason)`` from a ChatCompletion.

        Defensive against structured tool calls (which return
        ``content=None``); we coerce missing content to the empty string.
        """
        choices = getattr(completion, "choices", None) or []
        if not choices:
            return "", "stop"
        first = choices[0]
        message = getattr(first, "message", None)
        content = getattr(message, "content", None) if message else None
        if content is None and isinstance(message, dict):
            content = message.get("content")
        finish = getattr(first, "finish_reason", None) or "stop"
        return content or "", str(finish)

    @staticmethod
    def _extract_usage(completion: Any) -> tuple[int, int]:
        """Return ``(input_tokens, output_tokens)`` from a ChatCompletion."""
        usage = getattr(completion, "usage", None)
        if usage is None:
            return 0, 0
        in_tok = (
            getattr(usage, "prompt_tokens", None)
            or getattr(usage, "input_tokens", None)
            or 0
        )
        out_tok = (
            getattr(usage, "completion_tokens", None)
            or getattr(usage, "output_tokens", None)
            or 0
        )
        return int(in_tok or 0), int(out_tok or 0)

    @staticmethod
    def _to_dict(message: Any) -> dict[str, Any]:
        """Best-effort conversion to a plain dict for the ``raw`` field."""
        if hasattr(message, "model_dump"):
            try:
                return dict(message.model_dump())
            except Exception:  # noqa: BLE001
                pass
        if hasattr(message, "dict"):
            try:
                return dict(message.dict())  # type: ignore[call-arg]
            except Exception:  # noqa: BLE001
                pass
        if isinstance(message, dict):
            return dict(message)
        return {"raw": str(message)}

    # ------------------------------------------------------------------
    # Cost
    # ------------------------------------------------------------------

    def estimate_cost_usd(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        """Compute USD cost using :data:`OPENAI_PRICING`."""
        pricing = OPENAI_PRICING.get(model, _FALLBACK_PRICING)
        return input_tokens * pricing["input"] + output_tokens * pricing["output"]

    # ------------------------------------------------------------------
    # Error mapping
    # ------------------------------------------------------------------

    def _wrap_error(self, exc: BaseException) -> Exception:
        """Translate an SDK error to a Plinth :class:`LLMProviderError`."""
        if isinstance(exc, openai.RateLimitError):
            retry_after = _parse_retry_after(exc)
            return LLMRateLimited(
                str(exc),
                retry_after_seconds=retry_after,
                status_code=getattr(exc, "status_code", 429),
                body=_response_body(exc),
                provider=self.name,
            )
        if isinstance(exc, openai.APIStatusError):
            return LLMProviderError(
                str(exc),
                status_code=getattr(exc, "status_code", None),
                body=_response_body(exc),
                provider=self.name,
            )
        if isinstance(exc, openai.APIConnectionError):
            return LLMProviderError(
                f"connection error: {exc}",
                status_code=None,
                provider=self.name,
            )
        return LLMProviderError(str(exc), provider=self.name)

    # ------------------------------------------------------------------
    # Sync surface
    # ------------------------------------------------------------------

    def complete(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """One-shot chat completion."""
        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_chat(messages),
        }
        if max_tokens is not None:
            api_kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            api_kwargs["temperature"] = temperature
        api_kwargs.update(kwargs)

        try:
            completion = self._client.chat.completions.create(**api_kwargs)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc) from exc

        content, finish_reason = self._extract_content(completion)
        in_tok, out_tok = self._extract_usage(completion)
        return LLMResponse(
            content=content,
            model=getattr(completion, "model", model),
            finish_reason=finish_reason,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=self.estimate_cost_usd(model, in_tok, out_tok),
            duration_ms=0,
            provider=self.name,
            raw=self._to_dict(completion),
        )

    def stream(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        """Yield incremental chunks from the chat-completions stream."""
        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_chat(messages),
            "stream": True,
        }
        if max_tokens is not None:
            api_kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            api_kwargs["temperature"] = temperature
        api_kwargs.update(kwargs)

        try:
            response = self._client.chat.completions.create(**api_kwargs)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc) from exc

        finish_reason: str | None = None
        try:
            for chunk in response:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta_obj = getattr(choices[0], "delta", None)
                content = getattr(delta_obj, "content", None) if delta_obj else None
                if content is None and isinstance(delta_obj, dict):
                    content = delta_obj.get("content")
                fr = getattr(choices[0], "finish_reason", None)
                if fr is not None:
                    finish_reason = str(fr)
                if content:
                    yield LLMStreamChunk(delta=content)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc) from exc

        yield LLMStreamChunk(delta="", finish_reason=finish_reason or "stop")

    # ------------------------------------------------------------------
    # Async surface
    # ------------------------------------------------------------------

    async def acomplete(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Async equivalent of :meth:`complete`."""
        client = self._get_async()
        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_chat(messages),
        }
        if max_tokens is not None:
            api_kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            api_kwargs["temperature"] = temperature
        api_kwargs.update(kwargs)

        try:
            completion = await client.chat.completions.create(**api_kwargs)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc) from exc

        content, finish_reason = self._extract_content(completion)
        in_tok, out_tok = self._extract_usage(completion)
        return LLMResponse(
            content=content,
            model=getattr(completion, "model", model),
            finish_reason=finish_reason,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=self.estimate_cost_usd(model, in_tok, out_tok),
            duration_ms=0,
            provider=self.name,
            raw=self._to_dict(completion),
        )

    async def astream(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Async streaming counterpart of :meth:`stream`."""
        client = self._get_async()
        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_chat(messages),
            "stream": True,
        }
        if max_tokens is not None:
            api_kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            api_kwargs["temperature"] = temperature
        api_kwargs.update(kwargs)

        try:
            response = await client.chat.completions.create(**api_kwargs)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc) from exc

        finish_reason: str | None = None
        try:
            async for chunk in response:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta_obj = getattr(choices[0], "delta", None)
                content = getattr(delta_obj, "content", None) if delta_obj else None
                if content is None and isinstance(delta_obj, dict):
                    content = delta_obj.get("content")
                fr = getattr(choices[0], "finish_reason", None)
                if fr is not None:
                    finish_reason = str(fr)
                if content:
                    yield LLMStreamChunk(delta=content)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc) from exc

        yield LLMStreamChunk(delta="", finish_reason=finish_reason or "stop")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_retry_after(exc: BaseException) -> float | None:
    """Extract a ``retry-after`` hint from an OpenAI SDK error."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _response_body(exc: BaseException) -> Any:
    """Return the raw body text of the SDK error, when available."""
    body = getattr(exc, "body", None)
    if body is not None:
        return body
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            return response.text
        except Exception:  # noqa: BLE001
            return None
    return None


__all__ = ["OPENAI_PRICING", "OpenAIProvider"]
