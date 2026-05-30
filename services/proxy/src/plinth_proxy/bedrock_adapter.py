# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""AWS Bedrock ``Converse`` API → OpenAI ``/v1/chat/completions`` adapter.

Same two-direction pattern as :mod:`anthropic_adapter` and
:mod:`gemini_adapter`. Bedrock's runtime ``Converse`` API is the unified
message shape AWS exposes across every hosted model (Claude, Llama, Titan,
Mistral, Command …), so one adapter covers all of them. The wire path is
``POST /model/{modelId}/converse``.

Where the Converse schema differs from OpenAI (and even from the Anthropic
schema it superficially resembles):

* Messages are ``messages: [{role, content: [block, ...]}]`` with roles
  ``user`` / ``assistant``. There is no ``system`` role — system prompts
  live in a top-level ``system: [{text: "..."}]`` list of blocks.
* A tool call is a ``{"toolUse": {"toolUseId", "name", "input"}}`` block
  inside an ``assistant`` message (note camelCase ``toolUseId`` and the
  already-parsed ``input`` object — not a JSON string).
* A tool result is a ``{"toolResult": {"toolUseId", "content": [...],
  "status"}}`` block inside a ``user`` message. Result content is itself a
  list of blocks, each ``{"json": {...}}`` or ``{"text": "..."}``.
* Tool *definitions* are ``toolConfig: {tools: [{toolSpec: {name,
  description, inputSchema: {json: {...}}}}], toolChoice}``.
* Inference knobs live under ``inferenceConfig: {maxTokens, temperature,
  topP, stopSequences}``.
* Responses are ``{output: {message: {role, content: [...]}}, stopReason,
  usage: {inputTokens, outputTokens, totalTokens}}`` — ``stopReason`` uses
  ``end_turn | tool_use | max_tokens | stop_sequence | content_filtered``.

Both unary and streaming are exposed. ``ConverseStream`` is not HTTP SSE —
it frames events in the AWS event-stream *binary* protocol
(``vnd.amazon.eventstream``: a length/CRC32 prelude, typed headers, JSON
payload, trailing CRC32); the proxy runs the request unary (interception
must finish first) and re-emits the final body as that binary event sequence
(see ``_synthesize_bedrock_converse_stream`` in :mod:`.api`).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Request: Bedrock Converse → OpenAI
# ---------------------------------------------------------------------------


def bedrock_converse_request_to_openai(
    body: dict[str, Any], model: str | None = None
) -> dict[str, Any]:
    """Translate a Bedrock ``Converse`` request body into OpenAI's shape."""
    messages: list[dict[str, Any]] = []

    # 1. system: [{text}] → a single role:"system" message.
    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text_parts = [b.get("text", "") for b in system if isinstance(b, dict) and "text" in b]
        joined = "\n".join(p for p in text_parts if p)
        if joined:
            messages.append({"role": "system", "content": joined})

    # 2. Each Converse message → one or more OpenAI messages.
    for msg in body.get("messages") or []:
        messages.extend(_translate_message_request(msg))

    out: dict[str, Any] = {
        "model": model or body.get("modelId") or body.get("model") or "gpt-4o",
        "messages": messages,
    }

    # 3. toolConfig.tools[].toolSpec → OpenAI function tools.
    tool_config = body.get("toolConfig") or body.get("tool_config") or {}
    tools = tool_config.get("tools") or []
    fn_tools: list[dict[str, Any]] = []
    for t in tools:
        spec = t.get("toolSpec") or t.get("tool_spec") or {}
        if not spec:
            continue
        schema = spec.get("inputSchema") or spec.get("input_schema") or {}
        params = schema.get("json") if isinstance(schema, dict) else None
        fn_tools.append(
            {
                "type": "function",
                "function": {
                    "name": spec.get("name", ""),
                    "description": spec.get("description", ""),
                    "parameters": params or {"type": "object"},
                },
            }
        )
    if fn_tools:
        out["tools"] = fn_tools

    # 3b. toolChoice → OpenAI tool_choice.
    choice = tool_config.get("toolChoice") or tool_config.get("tool_choice")
    mapped_choice = _translate_tool_choice(choice)
    if mapped_choice is not None:
        out["tool_choice"] = mapped_choice

    # 4. inferenceConfig knobs.
    cfg = body.get("inferenceConfig") or body.get("inference_config") or {}
    if "maxTokens" in cfg:
        out["max_tokens"] = cfg["maxTokens"]
    elif "max_tokens" in cfg:
        out["max_tokens"] = cfg["max_tokens"]
    if "temperature" in cfg:
        out["temperature"] = cfg["temperature"]
    if "topP" in cfg:
        out["top_p"] = cfg["topP"]
    elif "top_p" in cfg:
        out["top_p"] = cfg["top_p"]
    stops = cfg.get("stopSequences") or cfg.get("stop_sequences")
    if stops:
        out["stop"] = stops

    return out


def _translate_message_request(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """One Converse message → list of OpenAI messages (tool results split out)."""
    role = msg.get("role", "user")
    content = msg.get("content")

    # Converse always uses a content-block list, but tolerate a bare string.
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    if not isinstance(content, list):
        return [{"role": role, "content": str(content or "")}]

    text_buf: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        if "text" in block:
            text_buf.append(block.get("text", "") or "")
        elif "toolUse" in block or "tool_use" in block:
            tu = block.get("toolUse") or block.get("tool_use") or {}
            tool_calls.append(
                {
                    "id": tu.get("toolUseId") or tu.get("tool_use_id")
                    or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": tu.get("name", ""),
                        # Converse hands us the input already parsed as an object.
                        "arguments": json.dumps(tu.get("input") or {}),
                    },
                }
            )
        elif "toolResult" in block or "tool_result" in block:
            tr = block.get("toolResult") or block.get("tool_result") or {}
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": tr.get("toolUseId") or tr.get("tool_use_id") or "",
                    "content": _tool_result_content_to_str(tr.get("content")),
                }
            )

    out: list[dict[str, Any]] = []
    # Tool results answer a previous assistant toolUse — emit them first.
    out.extend(tool_results)

    if role == "assistant" and (text_buf or tool_calls):
        msg_out: dict[str, Any] = {"role": "assistant"}
        msg_out["content"] = "".join(text_buf) if text_buf else None
        if tool_calls:
            msg_out["tool_calls"] = tool_calls
        out.append(msg_out)
    elif role == "user" and text_buf:
        out.append({"role": "user", "content": "".join(text_buf)})

    return out


def _tool_result_content_to_str(content: Any) -> str:
    """Flatten Converse toolResult content blocks into a single string.

    Each block is ``{"json": {...}}`` or ``{"text": "..."}``. The proxy's
    tool-call interception only needs a string body to shape, so we
    serialise JSON blocks and concatenate text blocks.
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
            elif "json" in blk:
                parts.append(json.dumps(blk["json"]))
            elif "text" in blk:
                parts.append(blk.get("text", "") or "")
        return "".join(parts)
    return json.dumps(content)


def _translate_tool_choice(choice: Any) -> Any:
    """Bedrock toolChoice → OpenAI tool_choice.

    Bedrock: ``{"auto": {}}`` | ``{"any": {}}`` | ``{"tool": {"name": "x"}}``.
    OpenAI:  ``"auto"`` | ``"required"`` | ``{"type": "function",
    "function": {"name": "x"}}``.
    """
    if not isinstance(choice, dict):
        return None
    if "auto" in choice:
        return "auto"
    if "any" in choice:
        return "required"
    if "tool" in choice:
        name = (choice.get("tool") or {}).get("name", "")
        if name:
            return {"type": "function", "function": {"name": name}}
    return None


# ---------------------------------------------------------------------------
# Response: OpenAI → Bedrock Converse
# ---------------------------------------------------------------------------


_STOP_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filtered",
}


def openai_response_to_bedrock_converse(resp: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI chat-completion result into a Converse response."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content_blocks: list[dict[str, Any]] = []

    text = msg.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"text": text})

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            inp = {}
        content_blocks.append(
            {
                "toolUse": {
                    "toolUseId": tc.get("id") or f"tooluse_{uuid.uuid4().hex[:12]}",
                    "name": fn.get("name", ""),
                    "input": inp,
                }
            }
        )

    finish = choice.get("finish_reason") or "stop"
    stop_reason = _STOP_REASON_MAP.get(finish, "end_turn")

    usage = resp.get("usage") or {}
    in_tok = usage.get("prompt_tokens", 0) or 0
    out_tok = usage.get("completion_tokens", 0) or 0

    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": content_blocks or [{"text": ""}],
            }
        },
        "stopReason": stop_reason,
        "usage": {
            "inputTokens": in_tok,
            "outputTokens": out_tok,
            "totalTokens": in_tok + out_tok,
        },
        "metrics": {"latencyMs": 0},
    }


__all__ = [
    "bedrock_converse_request_to_openai",
    "openai_response_to_bedrock_converse",
]
