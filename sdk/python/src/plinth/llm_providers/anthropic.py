# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Anthropic provider — wraps the official ``anthropic`` SDK.

The provider lives behind the optional ``[anthropic]`` extra:
``pip install 'plinth[anthropic]'``. If the package is missing the
import will fail and :func:`plinth.llm_providers.build_provider`
re-raises as :class:`~plinth.exceptions.LLMProviderNotInstalled` with
the install hint.

The cost helper uses the published per-million-token pricing for the
v4.5 generation. Exact figures are taken from Anthropic's pricing page.
Update :data:`ANTHROPIC_PRICING` when the published rates change — the
table is the single source of truth for cost calculation.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

from ..exceptions import (
    LLMProviderError,
    LLMRateLimited,
)
from ..models import LLMMessage, LLMResponse, LLMStreamChunk

# ``anthropic`` is an optional dependency. The import sits at module load
# so :func:`build_provider` can catch :class:`ImportError` and raise the
# typed :class:`LLMProviderNotInstalled`. Inside the provider we also
# import ``anthropic.APIStatusError`` for status-code mapping; that
# import is local to the call sites because it lives behind the same
# extra.
import anthropic  # type: ignore[import-untyped]  # noqa: E402


#: Per-token pricing in USD. Numbers are USD/token (not USD/Mtok), so
#: cost = tokens * rate.
#:
#: Source: anthropic.com/pricing (v4.5 series).
ANTHROPIC_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5": {
        "input": 3.00 / 1_000_000,
        "output": 15.00 / 1_000_000,
    },
    "claude-opus-4-5": {
        "input": 15.00 / 1_000_000,
        "output": 75.00 / 1_000_000,
    },
    "claude-haiku-4-5": {
        "input": 0.80 / 1_000_000,
        "output": 4.00 / 1_000_000,
    },
}

#: Fallback pricing applied when the model name is unknown. Picks the
#: Sonnet rate because it's the most common production target — keeps
#: budgeting roughly correct on new aliases the table hasn't seen yet.
_FALLBACK_PRICING: dict[str, float] = ANTHROPIC_PRICING["claude-sonnet-4-5"]


class AnthropicProvider:
    """Plinth's adapter over ``anthropic.Anthropic``.

    Args:
        api_key: Anthropic API key. Defaults to ``ANTHROPIC_API_KEY`` from
            the environment when omitted.
        base_url: Override the API base URL (rare; mostly for proxies).
        client_options: Extra keyword args forwarded to the underlying
            ``anthropic.Anthropic`` constructor — useful for custom
            HTTP clients in tests.
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client_options: dict[str, Any] | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        kwargs: dict[str, Any] = dict(client_options or {})
        if resolved_key:
            kwargs.setdefault("api_key", resolved_key)
        if base_url:
            kwargs.setdefault("base_url", base_url)
        self._client = anthropic.Anthropic(**kwargs)
        # Async client lazily constructed: ``anthropic.AsyncAnthropic``
        # opens its own connection pool and we don't want to pay for it
        # in sync-only programs.
        self._async_client: anthropic.AsyncAnthropic | None = None
        self._async_kwargs = kwargs

    def _get_async(self) -> anthropic.AsyncAnthropic:
        if self._async_client is None:
            self._async_client = anthropic.AsyncAnthropic(**self._async_kwargs)
        return self._async_client

    # ------------------------------------------------------------------
    # Message translation
    # ------------------------------------------------------------------

    @staticmethod
    def _split_system(
        messages: list[LLMMessage] | list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Split system messages out and reformat the rest.

        Anthropic's API takes the system prompt as a top-level argument
        and the rest as a ``messages`` list of ``{role, content}``. We
        concatenate multiple system messages with a blank line so callers
        can compose them without rebuilding the prompt themselves.
        """
        system_parts: list[str] = []
        chat: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.role if isinstance(msg, LLMMessage) else msg.get("role")
            content = (
                msg.content if isinstance(msg, LLMMessage) else msg.get("content", "")
            )
            if role == "system":
                if content:
                    system_parts.append(content)
                continue
            # Anthropic's role enum is ``user`` | ``assistant``. Tool
            # messages aren't supported on the message-level surface in
            # the simple-chat API path; we collapse any unknown role to
            # ``user`` so the call doesn't 400.
            api_role = role if role in ("user", "assistant") else "user"
            chat.append({"role": api_role, "content": content})
        system = "\n\n".join(system_parts) if system_parts else None
        return system, chat

    @staticmethod
    def _extract_content(message: Any) -> str:
        """Pull the assistant's text out of an Anthropic ``Message``.

        Anthropic returns ``content`` as a list of blocks; for plain
        text completions there's a single ``TextBlock``. We concatenate
        every text block and ignore other block types (which the simple
        completion path shouldn't produce, but defensively…).
        """
        content_blocks = getattr(message, "content", None) or []
        parts: list[str] = []
        for block in content_blocks:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)

    @staticmethod
    def _extract_usage(message: Any) -> tuple[int, int]:
        """Return ``(input_tokens, output_tokens)`` from a response."""
        usage = getattr(message, "usage", None)
        if usage is None:
            return 0, 0
        in_tok = (
            getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", 0)
        )
        out_tok = (
            getattr(usage, "output_tokens", None)
            or getattr(usage, "completion_tokens", 0)
        )
        return int(in_tok or 0), int(out_tok or 0)

    @staticmethod
    def _to_dict(message: Any) -> dict[str, Any]:
        """Best-effort conversion to a plain dict for ``raw``."""
        if hasattr(message, "model_dump"):
            try:
                return dict(message.model_dump())
            except Exception:  # noqa: BLE001 - best-effort serialization
                pass
        if hasattr(message, "dict"):
            try:
                return dict(message.dict())  # type: ignore[call-arg]
            except Exception:  # noqa: BLE001
                pass
        return {"raw": str(message)}

    # ------------------------------------------------------------------
    # Cost
    # ------------------------------------------------------------------

    def estimate_cost_usd(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        """Compute USD cost using :data:`ANTHROPIC_PRICING`."""
        pricing = ANTHROPIC_PRICING.get(model, _FALLBACK_PRICING)
        return input_tokens * pricing["input"] + output_tokens * pricing["output"]

    # ------------------------------------------------------------------
    # Error mapping
    # ------------------------------------------------------------------

    def _wrap_error(self, exc: BaseException) -> Exception:
        """Translate an SDK error to a Plinth :class:`LLMProviderError`."""
        # ``anthropic.RateLimitError`` is a 429 subclass on
        # ``APIStatusError``. Treat it specially so the retry loop knows
        # to honour ``Retry-After``.
        if isinstance(exc, anthropic.RateLimitError):
            retry_after = _parse_retry_after(exc)
            return LLMRateLimited(
                str(exc),
                retry_after_seconds=retry_after,
                status_code=getattr(exc, "status_code", 429),
                body=_response_body(exc),
                provider=self.name,
            )
        if isinstance(exc, anthropic.APIStatusError):
            return LLMProviderError(
                str(exc),
                status_code=getattr(exc, "status_code", None),
                body=_response_body(exc),
                provider=self.name,
            )
        if isinstance(exc, anthropic.APIConnectionError):
            return LLMProviderError(
                f"connection error: {exc}",
                status_code=None,
                provider=self.name,
            )
        # Unknown — wrap so downstream code can ``except LLMError``.
        return LLMProviderError(str(exc), provider=self.name)

    # ------------------------------------------------------------------
    # Sync surface
    # ------------------------------------------------------------------

    def complete(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        max_tokens: int = 1024,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """One-shot completion. Translates errors via :meth:`_wrap_error`."""
        system, chat = self._split_system(messages)
        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": chat,
            "max_tokens": max_tokens,
        }
        if system is not None:
            api_kwargs["system"] = system
        if temperature is not None:
            api_kwargs["temperature"] = temperature
        # Pass-through extras the caller wants (top_p, stop_sequences …).
        api_kwargs.update(kwargs)

        try:
            message = self._client.messages.create(**api_kwargs)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc) from exc

        text = self._extract_content(message)
        in_tok, out_tok = self._extract_usage(message)
        finish_reason = getattr(message, "stop_reason", None) or "stop"
        return LLMResponse(
            content=text,
            model=getattr(message, "model", model),
            finish_reason=str(finish_reason),
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=self.estimate_cost_usd(model, in_tok, out_tok),
            duration_ms=0,  # populated by LLMClient using its own clock
            provider=self.name,
            raw=self._to_dict(message),
        )

    def stream(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        max_tokens: int = 1024,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        """Yield incremental chunks from the Anthropic streaming API."""
        system, chat = self._split_system(messages)
        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": chat,
            "max_tokens": max_tokens,
        }
        if system is not None:
            api_kwargs["system"] = system
        if temperature is not None:
            api_kwargs["temperature"] = temperature
        api_kwargs.update(kwargs)

        try:
            with self._client.messages.stream(**api_kwargs) as stream:
                for text in stream.text_stream:
                    yield LLMStreamChunk(delta=text)
                final = stream.get_final_message()
                yield LLMStreamChunk(
                    delta="",
                    finish_reason=str(getattr(final, "stop_reason", "stop") or "stop"),
                    raw=self._to_dict(final),
                )
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc) from exc

    # ------------------------------------------------------------------
    # Async surface
    # ------------------------------------------------------------------

    async def acomplete(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        max_tokens: int = 1024,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Async equivalent of :meth:`complete`."""
        client = self._get_async()
        system, chat = self._split_system(messages)
        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": chat,
            "max_tokens": max_tokens,
        }
        if system is not None:
            api_kwargs["system"] = system
        if temperature is not None:
            api_kwargs["temperature"] = temperature
        api_kwargs.update(kwargs)

        try:
            message = await client.messages.create(**api_kwargs)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc) from exc

        text = self._extract_content(message)
        in_tok, out_tok = self._extract_usage(message)
        finish_reason = getattr(message, "stop_reason", None) or "stop"
        return LLMResponse(
            content=text,
            model=getattr(message, "model", model),
            finish_reason=str(finish_reason),
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=self.estimate_cost_usd(model, in_tok, out_tok),
            duration_ms=0,
            provider=self.name,
            raw=self._to_dict(message),
        )

    async def astream(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        max_tokens: int = 1024,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Async streaming counterpart of :meth:`stream`."""
        client = self._get_async()
        system, chat = self._split_system(messages)
        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": chat,
            "max_tokens": max_tokens,
        }
        if system is not None:
            api_kwargs["system"] = system
        if temperature is not None:
            api_kwargs["temperature"] = temperature
        api_kwargs.update(kwargs)

        try:
            async with client.messages.stream(**api_kwargs) as stream:
                async for text in stream.text_stream:
                    yield LLMStreamChunk(delta=text)
                final = await stream.get_final_message()
                yield LLMStreamChunk(
                    delta="",
                    finish_reason=str(getattr(final, "stop_reason", "stop") or "stop"),
                    raw=self._to_dict(final),
                )
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_retry_after(exc: BaseException) -> float | None:
    """Extract a ``retry-after`` hint from an SDK error, if present.

    Anthropic surfaces the header on ``exc.response`` (when available).
    A best-effort lookup keeps the SDK working when the upstream layout
    shifts between minor versions.
    """
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


__all__ = ["ANTHROPIC_PRICING", "AnthropicProvider"]
