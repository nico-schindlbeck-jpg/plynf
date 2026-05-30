# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Cohere v2 ``POST /v2/chat`` → OpenAI ``/v1/chat/completions`` adapter.

Same two-direction pattern as :mod:`anthropic_adapter`, :mod:`gemini_adapter`
and :mod:`bedrock_adapter`. Cohere's *v2* Chat API is deliberately close to
OpenAI's — a ``messages`` array, ``tools`` as ``{type: "function",
function: {...}}``, and ``tool_calls`` carrying ``{id, type, function:
{name, arguments}}`` with ``arguments`` as a JSON *string* — so the request
side is nearly pass-through. The meaningful divergences are:

* Message ``content`` may be a plain string OR a list of content blocks
  (``{"type": "text", "text": "..."}``); ``tool`` results may also arrive as
  ``{"type": "document", "document": {...}}`` blocks. We flatten all of these
  to a single string, which is what the shaping pipeline needs.
* Assistant turns may carry a ``tool_plan`` (Cohere's pre-tool reasoning).
  It has no OpenAI equivalent and is dropped on the way in.
* Top-p is spelled ``p`` (not ``top_p``); stops are ``stop_sequences``.
* ``tool_choice`` is the ALL-CAPS ``REQUIRED`` / ``NONE`` (no per-tool form).
* Responses wrap the assistant turn in ``message`` with a content-block list,
  an ALL-CAPS ``finish_reason`` (``COMPLETE`` / ``TOOL_CALL`` / ``MAX_TOKENS``
  …), and a nested ``usage: {billed_units, tokens}`` object.

Both unary and streaming are exposed. Cohere v2 streaming is a distinct
typed-event SSE taxonomy (``message-start`` → ``content-delta`` →
``message-end`` …); the proxy runs the request unary (interception must
finish first) and re-emits the final body as that event sequence (see
``_synthesize_cohere_sse`` in :mod:`.api`). The legacy v1 ``/v1/chat``
(``message`` + ``chat_history``) shape is intentionally not supported — v2
is Cohere's recommended surface.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Request: Cohere v2 → OpenAI
# ---------------------------------------------------------------------------


def cohere_chat_request_to_openai(
    body: dict[str, Any], model: str | None = None
) -> dict[str, Any]:
    """Translate a Cohere v2 ``/v2/chat`` request body into OpenAI's shape."""
    messages: list[dict[str, Any]] = []
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        messages.append(_translate_message_request(m))

    out: dict[str, Any] = {
        "model": model or body.get("model") or "gpt-4o",
        "messages": messages,
    }

    # tools — Cohere v2 already uses the OpenAI {type, function} envelope.
    fn_tools: list[dict[str, Any]] = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if not fn:
            continue
        fn_tools.append(
            {
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters") or {"type": "object"},
                },
            }
        )
    if fn_tools:
        out["tools"] = fn_tools

    mapped_choice = _translate_tool_choice(body.get("tool_choice") or body.get("toolChoice"))
    if mapped_choice is not None:
        out["tool_choice"] = mapped_choice

    # Inference knobs (Cohere spells top-p as "p", stops as "stop_sequences").
    if "temperature" in body:
        out["temperature"] = body["temperature"]
    if "max_tokens" in body:
        out["max_tokens"] = body["max_tokens"]
    if "p" in body:
        out["top_p"] = body["p"]
    elif "top_p" in body:
        out["top_p"] = body["top_p"]
    stops = body.get("stop_sequences") or body.get("stopSequences")
    if stops:
        out["stop"] = stops

    return out


def _translate_message_request(m: dict[str, Any]) -> dict[str, Any]:
    """One Cohere v2 message → one OpenAI message."""
    role = m.get("role", "user")

    if role == "tool":
        return {
            "role": "tool",
            "tool_call_id": m.get("tool_call_id") or m.get("toolCallId") or "",
            "content": _content_to_text(m.get("content")),
        }

    text = _content_to_text(m.get("content"))

    if role == "assistant":
        msg: dict[str, Any] = {"role": "assistant", "content": text or None}
        tool_calls = m.get("tool_calls") or m.get("toolCalls")
        if tool_calls:
            msg["tool_calls"] = _normalise_tool_calls(tool_calls)
        return msg

    # user / system (and any unknown role) → plain text content.
    return {"role": role, "content": text}


def _content_to_text(content: Any) -> str:
    """Flatten Cohere content (string or block list) into a single string.

    Blocks are ``{"type": "text", "text": ...}`` or, for tool results,
    ``{"type": "document", "document": {...}}``. Documents are serialised so
    the shaping pipeline always receives a string body.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if not isinstance(blk, dict):
                parts.append(str(blk))
            elif "text" in blk:
                parts.append(blk.get("text", "") or "")
            elif blk.get("type") == "document" or "document" in blk:
                doc = blk.get("document")
                data = doc.get("data", doc) if isinstance(doc, dict) else doc
                parts.append(data if isinstance(data, str) else json.dumps(data))
            else:
                parts.append(json.dumps(blk))
        return "".join(parts)
    return str(content)


def _normalise_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coerce Cohere tool_calls into OpenAI's exact shape.

    Cohere v2 already matches OpenAI, but we defensively ensure an ``id`` and
    a JSON-*string* ``arguments`` (some clients send a parsed object).
    """
    out: list[dict[str, Any]] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {})
        out.append(
            {
                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": fn.get("name", ""), "arguments": args},
            }
        )
    return out


def _translate_tool_choice(choice: Any) -> Any:
    """Cohere ``tool_choice`` (ALL-CAPS) → OpenAI ``tool_choice``."""
    if not isinstance(choice, str):
        return None
    return {
        "REQUIRED": "required",
        "NONE": "none",
        "AUTO": "auto",
        "required": "required",
        "none": "none",
        "auto": "auto",
    }.get(choice)


# ---------------------------------------------------------------------------
# Response: OpenAI → Cohere v2
# ---------------------------------------------------------------------------


# OpenAI finish_reason → Cohere v2 finish_reason (ALL-CAPS constants).
_FINISH_REASON_MAP = {
    "stop": "COMPLETE",
    "tool_calls": "TOOL_CALL",
    "length": "MAX_TOKENS",
    "content_filter": "ERROR_TOXIC",
}


def openai_response_to_cohere_chat(resp: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI chat-completion result into a Cohere v2 response."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}

    message: dict[str, Any] = {"role": "assistant"}

    text = msg.get("content")
    if isinstance(text, str) and text:
        message["content"] = [{"type": "text", "text": text}]

    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        message["tool_calls"] = _normalise_tool_calls(tool_calls)

    finish = choice.get("finish_reason") or "stop"
    finish_reason = _FINISH_REASON_MAP.get(finish, "COMPLETE")

    usage = resp.get("usage") or {}
    in_tok = usage.get("prompt_tokens", 0) or 0
    out_tok = usage.get("completion_tokens", 0) or 0

    return {
        "id": resp.get("id") or f"cohere-{uuid.uuid4().hex[:12]}",
        "finish_reason": finish_reason,
        "message": message,
        "usage": {
            "billed_units": {"input_tokens": in_tok, "output_tokens": out_tok},
            "tokens": {"input_tokens": in_tok, "output_tokens": out_tok},
        },
    }


__all__ = [
    "cohere_chat_request_to_openai",
    "openai_response_to_cohere_chat",
]
