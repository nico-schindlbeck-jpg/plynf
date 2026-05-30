# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""OpenAI *Responses* API ``POST /v1/responses`` → Chat-Completions adapter.

Same two-direction pattern as :mod:`anthropic_adapter`, :mod:`gemini_adapter`,
:mod:`bedrock_adapter` and :mod:`cohere_adapter`. The Responses API is OpenAI's
newer, agent-oriented surface (the strategic successor to Chat Completions),
so a growing share of OpenAI-native clients post here. Pointing such a client's
base URL at Plynf — no code change — should still get response-shaping savings.

The Responses shape diverges from Chat Completions in a few load-bearing ways:

* The prompt is ``input`` — either a plain string, or a list of *input items*.
  Items are messages (``{role, content}`` where ``content`` is a string or a
  list of ``{type: "input_text"|"output_text", text}`` parts), prior
  ``{type: "function_call", call_id, name, arguments}`` turns, or
  ``{type: "function_call_output", call_id, output}`` tool results.
* A top-level ``instructions`` string is the system prompt; the ``developer``
  role is the system role's new name.
* Function tools are *flat* — ``{type: "function", name, description,
  parameters}`` — not nested under a ``function`` key.
* ``max_output_tokens`` replaces ``max_tokens``.
* The response is an ``output`` array of items (``message`` with
  ``output_text`` parts, and/or ``function_call`` items), a convenience
  ``output_text`` aggregation, a ``status`` (``completed`` / ``incomplete``),
  and ``usage: {input_tokens, output_tokens, total_tokens}``.

Both unary and streaming are exposed. The Responses streaming protocol is a
distinct typed-event taxonomy (``response.created`` → ``response.output_text.
delta`` → ``response.completed`` …) unlike OpenAI chat SSE; the proxy runs the
request unary (interception must finish first) and re-emits the final body as
that event sequence (see ``_synthesize_responses_sse`` in :mod:`.api`).
Stateful features (``previous_response_id``, hosted tools such as web/file
search, reasoning items) are not supported — unknown item types are skipped
rather than erroring.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Request: OpenAI Responses → OpenAI Chat Completions
# ---------------------------------------------------------------------------


def responses_request_to_openai(
    body: dict[str, Any], model: str | None = None
) -> dict[str, Any]:
    """Translate an OpenAI ``/v1/responses`` request body into chat shape."""
    messages: list[dict[str, Any]] = []

    # Top-level instructions become the leading system message.
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})

    inp = body.get("input")
    if isinstance(inp, str):
        if inp:
            messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            _append_input_item(messages, item)

    out: dict[str, Any] = {
        "model": model or body.get("model") or "gpt-4o",
        "messages": messages,
    }

    fn_tools = _translate_tools(body.get("tools"))
    if fn_tools:
        out["tools"] = fn_tools

    choice = _translate_tool_choice(body.get("tool_choice"))
    if choice is not None:
        out["tool_choice"] = choice

    # Inference knobs. Responses spells the cap ``max_output_tokens``.
    max_out = body.get("max_output_tokens")
    if max_out is None:
        max_out = body.get("max_tokens")
    if max_out is not None:
        out["max_tokens"] = max_out
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]

    return out


def _append_input_item(messages: list[dict[str, Any]], item: Any) -> None:
    """Translate one Responses input item, appending 0..1 OpenAI messages."""
    if isinstance(item, str):
        messages.append({"role": "user", "content": item})
        return
    if not isinstance(item, dict):
        return

    itype = item.get("type")

    # Prior assistant tool call.
    if itype == "function_call":
        call_id = item.get("call_id") or item.get("id") or ""
        args = item.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {})
        tc = {
            "id": call_id,
            "type": "function",
            "function": {"name": item.get("name", ""), "arguments": args},
        }
        # Coalesce consecutive function_call items into a single assistant
        # turn — OpenAI chat carries all parallel tool_calls on one message.
        last = messages[-1] if messages else None
        if last and last.get("role") == "assistant" and last.get("tool_calls") is not None:
            last["tool_calls"].append(tc)
        else:
            messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
        return

    # Tool result.
    if itype == "function_call_output":
        messages.append(
            {
                "role": "tool",
                "tool_call_id": item.get("call_id") or "",
                "content": _output_to_text(item.get("output")),
            }
        )
        return

    # Message item (explicit type=="message" or a bare {role, content}).
    role = item.get("role")
    if role:
        mapped = "system" if role == "developer" else role
        messages.append({"role": mapped, "content": _content_to_text(item.get("content"))})


def _content_to_text(content: Any) -> str:
    """Flatten Responses message content (string or part list) to a string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, str):
                parts.append(blk)
            elif isinstance(blk, dict):
                # input_text / output_text / text all carry a "text" field.
                if "text" in blk:
                    parts.append(blk.get("text") or "")
                elif blk.get("type") == "refusal":
                    parts.append(blk.get("refusal") or "")
                # input_image / input_file have no text to extract → skipped.
        return "".join(parts)
    return str(content)


def _output_to_text(output: Any) -> str:
    """Flatten a ``function_call_output.output`` (string, parts, or object)."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return _content_to_text(output)
    return json.dumps(output)


def _translate_tools(tools: Any) -> list[dict[str, Any]]:
    """Responses flat function tools → OpenAI nested ``{type, function}`` tools.

    Hosted tools (``web_search``, ``file_search``, ``computer_use`` …) have no
    Plynf-side execution path and are skipped.
    """
    out: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        # Standard Responses tools are flat; tolerate clients that still nest.
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name") or t.get("name")
        if not name:
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": fn.get("description") or t.get("description") or "",
                    "parameters": fn.get("parameters")
                    or t.get("parameters")
                    or {"type": "object"},
                },
            }
        )
    return out


def _translate_tool_choice(choice: Any) -> Any:
    """Responses ``tool_choice`` → OpenAI ``tool_choice``."""
    if isinstance(choice, str):
        return choice if choice in ("auto", "none", "required") else None
    if isinstance(choice, dict) and choice.get("type") == "function":
        # Responses: {type:"function", name}; tolerate the nested chat form too.
        name = choice.get("name")
        if not name and isinstance(choice.get("function"), dict):
            name = choice["function"].get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    return None


# ---------------------------------------------------------------------------
# Response: OpenAI Chat Completions → OpenAI Responses
# ---------------------------------------------------------------------------


def openai_response_to_responses(resp: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI chat-completion result into a Responses body."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}

    output: list[dict[str, Any]] = []

    # Assistant text → a message item with an output_text content part. A
    # preamble (text alongside tool calls) precedes the function_call items,
    # mirroring OpenAI's own generation order.
    text = msg.get("content")
    output_text = text if isinstance(text, str) else ""
    if output_text:
        output.append(
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": output_text, "annotations": []}
                ],
            }
        )

    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {})
        output.append(
            {
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:24]}",
                "call_id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name", ""),
                "arguments": args,
                "status": "completed",
            }
        )

    finish = choice.get("finish_reason")
    status = "incomplete" if finish == "length" else "completed"

    usage = resp.get("usage") or {}
    in_tok = usage.get("prompt_tokens", 0) or 0
    out_tok = usage.get("completion_tokens", 0) or 0

    result: dict[str, Any] = {
        "id": resp.get("id") or f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": resp.get("created") or int(time.time()),
        "model": resp.get("model", ""),
        "status": status,
        "output": output,
        "output_text": output_text,
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
        },
    }
    if status == "incomplete":
        result["incomplete_details"] = {"reason": "max_output_tokens"}
    return result


__all__ = [
    "openai_response_to_responses",
    "responses_request_to_openai",
]
