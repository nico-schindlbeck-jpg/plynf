# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Deterministic LLM provider for tests + offline demos.

:class:`MockProvider` cycles through a list of canned responses and
shapes them into realistic :class:`~plinth.models.LLMResponse` objects
(token counts via :func:`plinth.tokens.count`, cost computed from the
hardcoded pricing in :data:`MOCK_PRICING`).

The provider runs with **zero network I/O**, which makes it the default
choice in:

* The full SDK test suite — no API keys required.
* Example 06's ``--mode=mock`` (default), so the demo runs cleanly on a
  laptop with nothing configured.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

from .. import tokens as tokens_module
from ..models import LLMMessage, LLMResponse, LLMStreamChunk

#: Per-million-token pricing used for cost estimates. Identical for input
#: and output so the math is obvious in tests; the real providers
#: override this with vendor pricing.
MOCK_PRICING: dict[str, dict[str, float]] = {
    "mock-default": {
        "input": 1.0 / 1_000_000,
        "output": 2.0 / 1_000_000,
    },
}


class MockProvider:
    """Cycles through a list of canned responses.

    Args:
        responses: An ordered list of responses to return on successive
            calls. Each item is either:

            * ``str`` — used directly as the assistant message content.
            * ``dict`` matching ``{"role": "assistant", "content": "..."}``
              — content extracted; other keys ignored.
            * Any other ``dict`` — passed through verbatim, falling back
              to ``str(dict)`` for the content if no ``content`` key.

            Cycling wraps around so the test harness doesn't need to
            count exact invocations.
        default_model: Model name reported in :class:`LLMResponse`.
            Defaults to ``"mock-model"``.
        finish_reason: Finish reason reported in responses. Defaults to
            ``"stop"``.
        chunk_size: Approximate characters per streaming chunk. Smaller
            values produce more chunks; the provider always emits at
            least one chunk per response.
    """

    name = "mock"

    def __init__(
        self,
        *,
        responses: list[str | dict[str, Any]] | None = None,
        default_model: str = "mock-model",
        finish_reason: str = "stop",
        chunk_size: int = 16,
    ) -> None:
        self._responses: list[str] = [
            self._normalize(r) for r in (responses or ["mock response"])
        ]
        self._default_model = default_model
        self._finish_reason = finish_reason
        self._chunk_size = max(1, int(chunk_size))
        self._cursor = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(item: str | dict[str, Any]) -> str:
        """Coerce a canned response item to a plain string."""
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            content = item.get("content")
            if isinstance(content, str):
                return content
            return str(item)
        return str(item)

    def _next(self) -> str:
        """Return the next canned response, cycling on exhaustion."""
        text = self._responses[self._cursor % len(self._responses)]
        self._cursor += 1
        return text

    @staticmethod
    def _input_text(messages: list[LLMMessage] | list[dict[str, Any]]) -> str:
        """Concatenate message contents for token-counting."""
        parts: list[str] = []
        for msg in messages:
            if isinstance(msg, LLMMessage):
                parts.append(msg.content)
            elif isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str):
                    parts.append(content)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Cost
    # ------------------------------------------------------------------

    def estimate_cost_usd(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        """Compute USD cost for a (model, tokens) tuple.

        Falls back to ``mock-default`` pricing when the model isn't in
        :data:`MOCK_PRICING` so unknown models still produce a sensible
        non-zero cost in tests.
        """
        pricing = MOCK_PRICING.get(model, MOCK_PRICING["mock-default"])
        return input_tokens * pricing["input"] + output_tokens * pricing["output"]

    # ------------------------------------------------------------------
    # Sync surface
    # ------------------------------------------------------------------

    def complete(
        self,
        *,
        model: str | None = None,
        messages: list[LLMMessage] | list[dict[str, Any]],
        max_tokens: int | None = None,  # noqa: ARG002 — accepted for API parity
        temperature: float | None = None,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> LLMResponse:
        """Return the next canned response as an :class:`LLMResponse`."""
        start = time.perf_counter()
        content = self._next()
        used_model = model or self._default_model
        input_text = self._input_text(messages)
        input_tokens = tokens_module.count(input_text)
        output_tokens = tokens_module.count(content)
        duration_ms = int((time.perf_counter() - start) * 1000)
        return LLMResponse(
            content=content,
            model=used_model,
            finish_reason=self._finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self.estimate_cost_usd(used_model, input_tokens, output_tokens),
            duration_ms=duration_ms,
            provider=self.name,
            raw={"mock": True, "content": content},
        )

    def stream(
        self,
        *,
        model: str | None = None,
        messages: list[LLMMessage] | list[dict[str, Any]],
        **kwargs: Any,  # noqa: ARG002
    ) -> Iterator[LLMStreamChunk]:
        """Yield the next canned response as ~``chunk_size``-char chunks.

        The terminal chunk carries ``finish_reason``; intermediate chunks
        leave it ``None`` so callers can stop iterating without checking
        every step.
        """
        del messages, model  # unused — kept for protocol parity
        content = self._next()
        if not content:
            yield LLMStreamChunk(delta="", finish_reason=self._finish_reason)
            return
        # Walk the content in fixed-size strides; produce a final chunk
        # carrying the finish_reason after all text has streamed.
        for i in range(0, len(content), self._chunk_size):
            yield LLMStreamChunk(delta=content[i : i + self._chunk_size])
        yield LLMStreamChunk(delta="", finish_reason=self._finish_reason)

    # ------------------------------------------------------------------
    # Async surface
    # ------------------------------------------------------------------

    async def acomplete(
        self,
        *,
        model: str | None = None,
        messages: list[LLMMessage] | list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        """Async wrapper — the mock has no I/O so this just sleeps 0."""
        await asyncio.sleep(0)
        return self.complete(model=model, messages=messages, **kwargs)

    async def astream(
        self,
        *,
        model: str | None = None,
        messages: list[LLMMessage] | list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Async generator equivalent of :meth:`stream`."""
        for chunk in self.stream(model=model, messages=messages, **kwargs):
            await asyncio.sleep(0)
            yield chunk


__all__ = ["MOCK_PRICING", "MockProvider"]
