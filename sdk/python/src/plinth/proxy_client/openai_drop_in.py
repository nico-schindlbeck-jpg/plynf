# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Drop-in OpenAI client that routes through the Plynf proxy.

Usage::

    from plinth.proxy_client import OpenAI

    client = OpenAI(api_key="sk-…", plynf_url="https://app.plynf.com")
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "What's order 12345?"}],
        tools=[{"type": "function", "function": {"name": "get_order"}}],
    )

The client is intentionally tiny: it speaks the OpenAI chat-completions
wire format and forwards every call to the configured Plynf proxy. We
do not re-import or re-export anything from the official ``openai``
package, so installing this SDK does not pull OpenAI as a dep.

If you need the real ``openai`` library for embeddings / images / audio,
keep using it — point its ``base_url`` at the same Plynf proxy URL and
both code paths benefit from the same shaping.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class _Completions:
    parent: OpenAI

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        stream: bool = False,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **extra: Any,
    ) -> dict[str, Any] | Iterable[dict[str, Any]]:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools is not None:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if response_format is not None:
            body["response_format"] = response_format
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        body["stream"] = bool(stream)
        body.update(extra)

        if stream:
            return self.parent._post_stream("/v1/chat/completions", body)
        return self.parent._post_json("/v1/chat/completions", body)


@dataclass
class _Chat:
    parent: OpenAI

    @property
    def completions(self) -> _Completions:
        return _Completions(parent=self.parent)


class OpenAI:
    """Drop-in replacement for ``openai.OpenAI`` that routes through Plynf.

    Only the surfaces our agents actually use are implemented — the chat
    completions endpoint and its streaming variant. Everything else (audio,
    images, embeddings) is unimplemented on purpose: pass those through to
    the real ``openai`` client with the Plynf base URL.
    """

    def __init__(
        self,
        *,
        api_key: str,
        plynf_url: str,
        timeout_s: float = 60.0,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self._base = plynf_url.rstrip("/")
        self._auth = f"Bearer {api_key}"
        self._timeout = timeout_s
        self._extra_headers = dict(default_headers or {})

    @property
    def chat(self) -> _Chat:
        return _Chat(parent=self)

    # -- internal HTTP helpers -------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Authorization": self._auth}
        h.update(self._extra_headers)
        return h

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(self._base + path, json=body, headers=self._headers())
        if resp.status_code >= 400:
            raise OpenAIProxyError(resp.status_code, resp.text)
        return resp.json()

    def _post_stream(self, path: str, body: dict[str, Any]) -> Iterable[dict[str, Any]]:
        """Yield decoded chunks from an SSE stream."""

        def _gen() -> Iterable[dict[str, Any]]:
            with httpx.Client(timeout=self._timeout) as client, client.stream(
                "POST", self._base + path, json=body, headers=self._headers()
            ) as resp:
                if resp.status_code >= 400:
                    raise OpenAIProxyError(resp.status_code, resp.read().decode("utf-8"))
                for raw in resp.iter_lines():
                    if not raw or not raw.startswith("data:"):
                        continue
                    payload = raw[len("data:") :].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        continue

        return _gen()


class OpenAIProxyError(RuntimeError):
    """Raised when the Plynf proxy returns a non-2xx response."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"plynf proxy returned {status}: {body[:300]}")
        self.status = status
        self.body = body


__all__ = ["OpenAI", "OpenAIProxyError"]
