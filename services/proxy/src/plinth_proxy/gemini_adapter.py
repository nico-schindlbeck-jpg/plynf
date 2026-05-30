# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Google Gemini ``/v1beta/{model}:generateContent`` → OpenAI adapter.

Same pattern as :mod:`anthropic_adapter`. Gemini's schema differs from
OpenAI in three meaningful ways:

* Messages are ``contents: [{role, parts: [...]}]``. Roles are
  ``user`` / ``model`` (not ``assistant``); there is no ``system`` role —
  system prompts go in a top-level ``systemInstruction`` field with the
  same ``parts`` shape.
* Tool calls return as ``parts: [{functionCall: {name, args}}]`` content
  parts inside a ``model`` message. Tool results come back as
  ``parts: [{functionResponse: {name, response}}]`` content parts inside
  a ``user`` message.
* Tool *definitions* are ``tools: [{functionDeclarations: [...]}]`` —
  note the nesting under ``functionDeclarations``.

Streaming uses ``streamGenerateContent``: with ``?alt=sse`` Gemini frames it
as SSE (``data: {GenerateContentResponse}``), and as a JSON array of responses
otherwise. The proxy serves both, synthesized from the unary result. Vertex
AI's API surface is a superset of public Gemini — same body shape, different
base URL — so the adapter works for both.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

# Map OpenAI finish_reason → Gemini finishReason (the inverse of the
# Anthropic map). Gemini uses uppercase ALL-CAPS constants.
_FINISH_REASON_MAP = {
    "stop": "STOP",
    "tool_calls": "STOP",  # Gemini doesn't have a distinct tool stop reason
    "length": "MAX_TOKENS",
    "content_filter": "SAFETY",
}


def gemini_request_to_openai(body: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    """Translate a Gemini ``generateContent`` body into OpenAI's shape."""
    messages: list[dict[str, Any]] = []

    # 1. systemInstruction → role: "system"
    sys_inst = body.get("systemInstruction") or body.get("system_instruction")
    if sys_inst:
        text = _parts_to_text(sys_inst.get("parts") or [])
        if text:
            messages.append({"role": "system", "content": text})

    # 2. Each Gemini message → one or more OpenAI messages.
    for c in body.get("contents") or []:
        messages.extend(_translate_content(c))

    out: dict[str, Any] = {
        "model": model or body.get("model") or "gpt-4o",
        "messages": messages,
    }

    # 3. tools → OpenAI function tools.
    tools = body.get("tools") or []
    fn_decls: list[dict[str, Any]] = []
    for t in tools:
        for fd in t.get("functionDeclarations") or t.get("function_declarations") or []:
            fn_decls.append(
                {
                    "type": "function",
                    "function": {
                        "name": fd.get("name", ""),
                        "description": fd.get("description", ""),
                        "parameters": fd.get("parameters") or {"type": "object"},
                    },
                }
            )
    if fn_decls:
        out["tools"] = fn_decls

    # 4. Generation config knobs.
    cfg = body.get("generationConfig") or body.get("generation_config") or {}
    if "maxOutputTokens" in cfg:
        out["max_tokens"] = cfg["maxOutputTokens"]
    if "max_output_tokens" in cfg:
        out["max_tokens"] = cfg["max_output_tokens"]
    if "temperature" in cfg:
        out["temperature"] = cfg["temperature"]

    return out


def _parts_to_text(parts: list[dict[str, Any]]) -> str:
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)


def _translate_content(c: dict[str, Any]) -> list[dict[str, Any]]:
    role = c.get("role", "user")
    parts = c.get("parts") or []
    text_buf: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []

    for part in parts:
        if not isinstance(part, dict):
            continue
        if "text" in part:
            text_buf.append(part["text"] or "")
        elif "functionCall" in part or "function_call" in part:
            fc = part.get("functionCall") or part.get("function_call") or {}
            tool_calls.append(
                {
                    "id": fc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args") or {}),
                    },
                }
            )
        elif "functionResponse" in part or "function_response" in part:
            fr = part.get("functionResponse") or part.get("function_response") or {}
            response = fr.get("response")
            content_str = response if isinstance(response, str) else json.dumps(response)
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": fr.get("name", "") or f"call_{uuid.uuid4().hex[:12]}",
                    "name": fr.get("name", ""),
                    "content": content_str,
                }
            )

    out: list[dict[str, Any]] = []
    out.extend(tool_results)

    if role == "model" and (text_buf or tool_calls):
        msg: dict[str, Any] = {"role": "assistant"}
        msg["content"] = "".join(text_buf) if text_buf else None
        if tool_calls:
            msg["tool_calls"] = tool_calls
        out.append(msg)
    elif role == "user" and text_buf:
        out.append({"role": "user", "content": "".join(text_buf)})

    return out


def openai_response_to_gemini(resp: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI chat-completion result into a Gemini candidate."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    parts: list[dict[str, Any]] = []

    if isinstance(msg.get("content"), str) and msg["content"]:
        parts.append({"text": msg["content"]})

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        parts.append(
            {
                "functionCall": {
                    "name": fn.get("name", ""),
                    "args": args,
                }
            }
        )

    finish = choice.get("finish_reason") or "stop"
    finish_reason = _FINISH_REASON_MAP.get(finish, "STOP")

    usage = resp.get("usage") or {}
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts or [{"text": ""}]},
                "finishReason": finish_reason,
                "index": 0,
                "safetyRatings": [],
            }
        ],
        "promptFeedback": {"safetyRatings": []},
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": (usage.get("prompt_tokens") or 0)
            + (usage.get("completion_tokens") or 0),
        },
        "modelVersion": resp.get("model") or "",
    }


__all__ = [
    "gemini_request_to_openai",
    "openai_response_to_gemini",
]
