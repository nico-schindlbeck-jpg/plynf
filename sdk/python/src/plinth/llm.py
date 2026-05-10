# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""The Plinth LLM facade.

The :class:`LLMClient` wraps a pluggable :class:`LLMProvider`, adds a
retry loop with exponential back-off + ``Retry-After`` honouring, and
records each call into the gateway audit log so cost shows up on the
existing Prometheus + dashboard pipeline.

The provider abstraction is small on purpose: ``complete`` /
``stream`` (sync), ``acomplete`` / ``astream`` (async), and
``estimate_cost_usd``. Adding a new vendor (Gemini, Mistral, Bedrock …)
is a single file in :mod:`plinth.llm_providers`.

Audit attribution: each successful LLM call posts to
``POST /v1/audit/record-llm`` on the gateway with a synthetic
``tool_id="llm.<provider>"``. Existing dashboards keying on tool_id
prefixes pick this up automatically. Failures of the audit POST are
swallowed — an LLM call must never fail because the audit endpoint is
unreachable.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from .exceptions import (
    LLMProviderError,
    LLMProviderNotConfigured,
    LLMRateLimited,
    LLMRetryExhausted,
)
from .llm_providers import build_provider
from .models import LLMMessage, LLMResponse, LLMStreamChunk

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import Plinth


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """The minimum surface a Plinth LLM provider must implement.

    The protocol is :func:`runtime_checkable` so tests can use
    ``isinstance`` against duck-typed mocks.
    """

    name: str

    def complete(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        ...

    def stream(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        ...

    async def acomplete(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        ...

    def astream(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        ...

    def estimate_cost_usd(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        ...


ProviderName = Literal["anthropic", "openai", "mock"]


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------


class LLMClient:
    """The ``client.llm`` namespace.

    Owns one :class:`LLMProvider` and exposes the user-facing
    ``complete`` / ``stream`` / ``acomplete`` / ``astream`` methods on
    top of it. Adds:

    * Retry-with-back-off on 429 (``Retry-After``-aware) and 5xx.
    * Audit-event recording on success via the gateway's
      ``/v1/audit/record-llm`` endpoint.
    * Auto-detection: if no provider is configured but
      ``ANTHROPIC_API_KEY`` (or ``OPENAI_API_KEY``) is set, the first
      call lazily configures the matching built-in.

    Args:
        plinth_client: The owning :class:`~plinth.client.Plinth` facade.
            We need it to reach the gateway HTTP client for audit.
        retries: Maximum retry attempts on retryable errors. ``0``
            disables retries entirely.
        retry_backoff_seconds: Base for exponential back-off — the
            actual wait is ``base * 2**attempt``. Capped at 30s.
    """

    def __init__(
        self,
        plinth_client: "Plinth",
        *,
        retries: int = 3,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self._plinth = plinth_client
        self._provider: LLMProvider | None = None
        self._retries = max(0, int(retries))
        self._retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))

    # ------------------------------------------------------------------
    # Provider configuration
    # ------------------------------------------------------------------

    @property
    def provider(self) -> LLMProvider | None:
        """Return the active provider (``None`` if not configured)."""
        return self._provider

    def use_provider(
        self,
        name: ProviderName | str,
        **config: Any,
    ) -> LLMProvider:
        """Configure one of the built-in providers by name.

        Returns the configured provider so callers can chain or hold a
        reference for inspection.
        """
        provider = build_provider(name, **config)
        self._provider = provider
        return provider

    def use_custom_provider(self, provider: LLMProvider) -> None:
        """Plug in a custom provider implementing :class:`LLMProvider`.

        Useful for integrating an in-house gateway, a non-built-in
        vendor, or a thin recording wrapper for tests.
        """
        self._provider = provider

    def _ensure_provider(self) -> LLMProvider:
        """Return the active provider, attempting auto-detection first.

        If no provider has been explicitly configured but the
        environment exposes ``ANTHROPIC_API_KEY`` or ``OPENAI_API_KEY``,
        we lazily build the matching built-in. Anthropic wins on a tie
        (matches Plinth's house style — Claude-first).
        """
        if self._provider is not None:
            return self._provider
        if os.environ.get("ANTHROPIC_API_KEY"):
            return self.use_provider("anthropic")
        if os.environ.get("OPENAI_API_KEY"):
            return self.use_provider("openai")
        raise LLMProviderNotConfigured(
            "No LLM provider configured. Call client.llm.use_provider("
            "'anthropic'|'openai'|'mock') first, or set "
            "ANTHROPIC_API_KEY / OPENAI_API_KEY in the environment."
        )

    # ------------------------------------------------------------------
    # Settings (post-construction tweaks for tests)
    # ------------------------------------------------------------------

    def configure_retries(
        self,
        *,
        retries: int | None = None,
        backoff_seconds: float | None = None,
    ) -> None:
        """Tune the retry loop without rebuilding the client."""
        if retries is not None:
            self._retries = max(0, int(retries))
        if backoff_seconds is not None:
            self._retry_backoff_seconds = max(0.0, float(backoff_seconds))

    # ------------------------------------------------------------------
    # Retry loop
    # ------------------------------------------------------------------

    @staticmethod
    def _is_retryable_status(status: int | None) -> bool:
        """Return True for 5xx errors (excluding 4xx other than 429)."""
        if status is None:
            return False
        return 500 <= status < 600

    def _backoff_for(self, attempt: int) -> float:
        """Compute the back-off delay for ``attempt`` (0-indexed)."""
        delay = self._retry_backoff_seconds * (2**attempt)
        return min(delay, 30.0)

    def _retry_sync(self, fn, *args: Any, **kwargs: Any) -> Any:
        """Run ``fn`` with retry on 429/5xx. Re-raises after exhaustion."""
        last_exc: BaseException | None = None
        for attempt in range(self._retries + 1):
            try:
                return fn(*args, **kwargs)
            except LLMRateLimited as exc:
                last_exc = exc
                if attempt >= self._retries:
                    break
                wait = exc.retry_after_seconds
                if wait is None or wait <= 0:
                    wait = self._backoff_for(attempt)
                time.sleep(wait)
                continue
            except LLMProviderError as exc:
                last_exc = exc
                if (
                    not self._is_retryable_status(exc.status_code)
                    or attempt >= self._retries
                ):
                    raise
                time.sleep(self._backoff_for(attempt))
                continue
        # Exhausted: wrap the last error to differentiate from a
        # one-shot failure that bubbled up directly.
        raise LLMRetryExhausted(
            f"LLM call failed after {self._retries + 1} attempts: {last_exc}",
            details={"attempts": self._retries + 1},
        ) from last_exc

    async def _retry_async(self, fn, *args: Any, **kwargs: Any) -> Any:
        """Async variant of :meth:`_retry_sync`."""
        last_exc: BaseException | None = None
        for attempt in range(self._retries + 1):
            try:
                return await fn(*args, **kwargs)
            except LLMRateLimited as exc:
                last_exc = exc
                if attempt >= self._retries:
                    break
                wait = exc.retry_after_seconds
                if wait is None or wait <= 0:
                    wait = self._backoff_for(attempt)
                await asyncio.sleep(wait)
                continue
            except LLMProviderError as exc:
                last_exc = exc
                if (
                    not self._is_retryable_status(exc.status_code)
                    or attempt >= self._retries
                ):
                    raise
                await asyncio.sleep(self._backoff_for(attempt))
                continue
        raise LLMRetryExhausted(
            f"LLM call failed after {self._retries + 1} attempts: {last_exc}",
            details={"attempts": self._retries + 1},
        ) from last_exc

    # ------------------------------------------------------------------
    # Audit recording
    # ------------------------------------------------------------------

    def _audit_payload(
        self,
        response: LLMResponse,
        *,
        workspace_id: str | None,
        agent_id: str | None,
    ) -> dict[str, Any]:
        """Build the body for ``/v1/audit/record-llm``."""
        return {
            "tool_id": f"llm.{response.provider}",
            "model": response.model,
            "input_tokens": int(response.input_tokens),
            "output_tokens": int(response.output_tokens),
            "cost_usd": float(response.cost_usd),
            "duration_ms": int(response.duration_ms),
            "workspace_id": workspace_id,
            "agent_id": agent_id,
            "finish_reason": response.finish_reason,
        }

    def _record_audit(
        self,
        response: LLMResponse,
        *,
        workspace_id: str | None,
        agent_id: str | None,
    ) -> None:
        """Best-effort audit POST. Mutates ``response.audit_id`` on success.

        Failures here are swallowed — the LLM call has already happened
        and the user's program should not crash because of an
        observability blip. A debug log would be appropriate but the
        SDK avoids configuring loggers eagerly; see
        :class:`~plinth.llm.LLMClient` for the design note.
        """
        gateway_http = getattr(self._plinth, "_gateway_http", None)
        if gateway_http is None:
            return
        body = self._audit_payload(
            response,
            workspace_id=workspace_id,
            agent_id=agent_id,
        )
        try:
            resp = gateway_http.post("/v1/audit/record-llm", json=body)
            data = resp.json()
            audit_id = data.get("audit_id") if isinstance(data, dict) else None
            if audit_id:
                response.audit_id = str(audit_id)
        except Exception:  # noqa: BLE001 - audit must never fail the call
            return

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
        workspace_id: str | None = None,
        agent_id: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Run a non-streaming LLM completion."""
        provider = self._ensure_provider()
        start = time.perf_counter()
        response = self._retry_sync(
            self._invoke_complete,
            provider,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs,
        )
        # Always re-time at the facade so duration_ms is consistent
        # across providers (some providers return 0 because they don't
        # measure inside their adapter).
        response.duration_ms = int((time.perf_counter() - start) * 1000)
        self._record_audit(
            response,
            workspace_id=workspace_id,
            agent_id=agent_id,
        )
        return response

    @staticmethod
    def _invoke_complete(
        provider: LLMProvider,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float | None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Internal: call ``provider.complete`` skipping ``None`` kwargs.

        Provider implementations have inconsistent defaults for
        ``max_tokens`` (Anthropic requires it; OpenAI doesn't), so we
        only forward what the caller actually set.
        """
        forward: dict[str, Any] = dict(kwargs)
        if max_tokens is not None:
            forward["max_tokens"] = max_tokens
        if temperature is not None:
            forward["temperature"] = temperature
        return provider.complete(
            model=model,
            messages=messages,
            **forward,
        )

    def stream(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        workspace_id: str | None = None,
        agent_id: str | None = None,
        **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        """Stream chunks; record audit after the iterator drains."""
        provider = self._ensure_provider()
        start = time.perf_counter()
        forward: dict[str, Any] = dict(kwargs)
        if max_tokens is not None:
            forward["max_tokens"] = max_tokens
        if temperature is not None:
            forward["temperature"] = temperature

        accumulated: list[str] = []
        finish_reason: str | None = None
        last_raw: dict[str, Any] = {}
        # Streaming bypasses :meth:`_retry_sync` because by the time we
        # know there's a transient failure, we've already started
        # emitting chunks to the user. Retry policy for streaming is
        # documented as "no automatic retries — caller restarts".
        try:
            for chunk in provider.stream(
                model=model,
                messages=messages,
                **forward,
            ):
                if chunk.delta:
                    accumulated.append(chunk.delta)
                if chunk.finish_reason is not None:
                    finish_reason = chunk.finish_reason
                if chunk.raw:
                    last_raw = chunk.raw
                yield chunk
        except Exception:
            raise

        duration_ms = int((time.perf_counter() - start) * 1000)
        text = "".join(accumulated)
        from . import tokens as _tokens

        # We don't always get usage from streaming; fall back to local
        # token counting via tiktoken so cost still flows through audit.
        input_tokens = _approx_input_tokens(messages)
        output_tokens = _tokens.count(text)
        cost = provider.estimate_cost_usd(model, input_tokens, output_tokens)
        synthesized = LLMResponse(
            content=text,
            model=model,
            finish_reason=finish_reason or "stop",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            duration_ms=duration_ms,
            provider=provider.name,
            raw=last_raw,
        )
        self._record_audit(
            synthesized,
            workspace_id=workspace_id,
            agent_id=agent_id,
        )

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
        workspace_id: str | None = None,
        agent_id: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Async non-streaming completion."""
        provider = self._ensure_provider()
        start = time.perf_counter()
        response = await self._retry_async(
            self._ainvoke_complete,
            provider,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs,
        )
        response.duration_ms = int((time.perf_counter() - start) * 1000)
        # Audit recording is sync HTTP — fine for v1.2; an async audit
        # surface can come later if profiling shows it matters.
        self._record_audit(
            response,
            workspace_id=workspace_id,
            agent_id=agent_id,
        )
        return response

    @staticmethod
    async def _ainvoke_complete(
        provider: LLMProvider,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float | None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Internal: call ``provider.acomplete`` with kwargs cleanup."""
        forward: dict[str, Any] = dict(kwargs)
        if max_tokens is not None:
            forward["max_tokens"] = max_tokens
        if temperature is not None:
            forward["temperature"] = temperature
        return await provider.acomplete(
            model=model,
            messages=messages,
            **forward,
        )

    async def astream(
        self,
        *,
        model: str,
        messages: list[LLMMessage] | list[dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        workspace_id: str | None = None,
        agent_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Async streaming counterpart of :meth:`stream`."""
        provider = self._ensure_provider()
        start = time.perf_counter()
        forward: dict[str, Any] = dict(kwargs)
        if max_tokens is not None:
            forward["max_tokens"] = max_tokens
        if temperature is not None:
            forward["temperature"] = temperature

        accumulated: list[str] = []
        finish_reason: str | None = None
        last_raw: dict[str, Any] = {}
        async for chunk in provider.astream(
            model=model,
            messages=messages,
            **forward,
        ):
            if chunk.delta:
                accumulated.append(chunk.delta)
            if chunk.finish_reason is not None:
                finish_reason = chunk.finish_reason
            if chunk.raw:
                last_raw = chunk.raw
            yield chunk

        duration_ms = int((time.perf_counter() - start) * 1000)
        text = "".join(accumulated)
        from . import tokens as _tokens

        input_tokens = _approx_input_tokens(messages)
        output_tokens = _tokens.count(text)
        cost = provider.estimate_cost_usd(model, input_tokens, output_tokens)
        synthesized = LLMResponse(
            content=text,
            model=model,
            finish_reason=finish_reason or "stop",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            duration_ms=duration_ms,
            provider=provider.name,
            raw=last_raw,
        )
        self._record_audit(
            synthesized,
            workspace_id=workspace_id,
            agent_id=agent_id,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _approx_input_tokens(
    messages: list[LLMMessage] | list[dict[str, Any]],
) -> int:
    """Rough offline input-token count via :mod:`plinth.tokens`."""
    from . import tokens as _tokens

    parts: list[str] = []
    for msg in messages:
        if isinstance(msg, LLMMessage):
            parts.append(msg.content)
        elif isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
    return _tokens.count("\n".join(parts))


__all__ = ["LLMClient", "LLMProvider", "ProviderName"]
