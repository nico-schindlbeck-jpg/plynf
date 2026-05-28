# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Anthropic ``/v1/messages`` → OpenAI ``/v1/chat/completions`` adapter.

Two translation directions:

* :func:`anthropic_request_to_openai` — incoming Anthropic-shaped request
  becomes the OpenAI shape the proxy's existing pipeline already handles.
* :func:`openai_response_to_anthropic` — final OpenAI response (after Plynf
  tool-call interception) is translated back into Anthropic's message
  schema so the client sees the wire format it expects.

Differences this module bridges:

* Anthropic puts the system message in a top-level ``system`` field; OpenAI
  puts it inside ``messages`` as ``role: "system"``.
* Anthropic tool results are content blocks inside a ``user`` message
  (``{"type":"tool_result","tool_use_id":"...","content":"..."}``); OpenAI
  uses a top-level ``role: "tool"`` message.
* Anthropic responses return content as ``content: [{type, text|tool_use}]``;
  OpenAI returns ``choices[].message.{content, tool_calls}``.
* Anthropic uses ``stop_reason``: ``"end_turn" | "tool_use" | …``; OpenAI
  uses ``finish_reason``: ``"stop" | "tool_calls" | …``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

# OpenAI tool_calls.id maps onto Anthropic tool_use.id one-to-one, so the
# round-trip preserves identity. We only mint new IDs for blocks we synthesise.


def anthropic_request_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic ``/v1/messages`` body into OpenAI's shape."""
    messages: list[dict[str, Any]] = []

    # 1. system → role: "system" prepended.
    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        # Anthropic also allows system as an array of content blocks.
        text_parts = [b.get("text", "") for b in system if b.get("type") == "text"]
        if text_parts:
            messages.append({"role": "system", "content": "\n".join(text_parts)})

    # 2. Each Anthropic message becomes one or more OpenAI messages.
    for msg in body.get("messages") or []:
        messages.extend(_translate_message_request(msg))

    out: dict[str, Any] = {
        "model": body.get("model") or "gpt-4o",
        "messages": messages,
    }

    # 3. tools (Anthropic shape) → OpenAI function tools.
    tools = body.get("tools")
    if tools:
        out["tools"] = [_translate_tool_definition(t) for t in tools]

    # 4. Optional knobs.
    if "max_tokens" in body:
        out["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        out["temperature"] = body["temperature"]
    if "stream" in body:
        out["stream"] = body["stream"]
    if "tool_choice" in body:
        out["tool_choice"] = body["tool_choice"]

    return out


def _translate_message_request(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """One Anthropic message → list of OpenAI messages (tool results split out)."""
    role = msg.get("role", "user")
    content = msg.get("content")

    # Simple string content → straight pass-through.
    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if not isinstance(content, list):
        return [{"role": role, "content": str(content or "")}]

    text_buf: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []

    for block in content:
        btype = block.get("type")
        if btype == "text":
            text_buf.append(block.get("text", ""))
        elif btype == "tool_use" and role == "assistant":
            tool_calls.append(
                {
                    "id": block.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
        elif btype == "tool_result" and role == "user":
            tool_content = block.get("content")
            if isinstance(tool_content, list):
                tool_content = "".join(
                    c.get("text", "") for c in tool_content if c.get("type") == "text"
                )
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": tool_content if isinstance(tool_content, str)
                    else json.dumps(tool_content),
                }
            )

    out: list[dict[str, Any]] = []

    # Tool results come first (they answer a previous assistant tool_use).
    out.extend(tool_results)

    if role == "assistant" and (text_buf or tool_calls):
        msg_out: dict[str, Any] = {"role": "assistant"}
        if text_buf:
            msg_out["content"] = "".join(text_buf)
        else:
            msg_out["content"] = None
        if tool_calls:
            msg_out["tool_calls"] = tool_calls
        out.append(msg_out)
    elif role == "user" and text_buf:
        out.append({"role": "user", "content": "".join(text_buf)})

    return out


def _translate_tool_definition(t: dict[str, Any]) -> dict[str, Any]:
    """Anthropic ``{name, description, input_schema}`` → OpenAI function tool."""
    return {
        "type": "function",
        "function": {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object"},
        },
    }


# ---------------------------------------------------------------------------
# Response: OpenAI → Anthropic
# ---------------------------------------------------------------------------


_STOP_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "stop_sequence",
}


def openai_response_to_anthropic(resp: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI chat-completion result into an Anthropic message."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content_blocks: list[dict[str, Any]] = []

    text = msg.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            inp = {}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                "name": fn.get("name", ""),
                "input": inp,
            }
        )

    finish = choice.get("finish_reason") or "stop"
    stop_reason = _STOP_REASON_MAP.get(finish, finish)

    return {
        "id": "msg_" + (resp.get("id") or uuid.uuid4().hex)[:24],
        "type": "message",
        "role": "assistant",
        "content": content_blocks or [{"type": "text", "text": ""}],
        "model": resp.get("model") or "",
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": (resp.get("usage") or {}).get("prompt_tokens", 0),
            "output_tokens": (resp.get("usage") or {}).get("completion_tokens", 0),
        },
    }


__all__ = [
    "anthropic_request_to_openai",
    "openai_response_to_anthropic",
]
