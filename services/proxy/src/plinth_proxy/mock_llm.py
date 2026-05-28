# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Deterministic mock LLM for the demo / offline mode.

This is NOT a model implementation. It's a scripted state machine that
behaves like an OpenAI-compatible chat-completions response — including
``tool_calls`` — so the proxy → policy → savings pipeline can be exercised
without an OpenAI API key.

Behaviour:

1. First call (only user message): returns a ``tool_calls`` response asking
   to invoke ``get_order`` (or another tool inferred from the user prompt).
2. Second call (after a ``role: tool`` message is appended): returns a
   plain-text summary that references fields from the shaped tool response.

To swap in a real upstream, set ``PLINTH_PROXY_UPSTREAM_BASE_URL`` — the
proxy will then forward verbatim and ignore this module.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

# Heuristics: pick a tool based on keywords in the user message. This is the
# minimum amount of intelligence we need to make the demo show interesting
# behaviour without an LLM.
_TOOL_KEYWORDS: list[tuple[str, str, dict[str, Any]]] = [
    ("order", "get_order", {"order_id": "12345"}),
    ("lead", "get_lead", {"lead_id": "00Q1aB"}),
    ("account", "get_account", {"account_id": "001..."}),
    ("opportunity", "get_opportunity", {"opp_id": "006..."}),
    ("channel", "get_channel_messages", {"channel": "general", "limit": 10}),
    ("user info", "get_user_info", {"user_id": "U123"}),
]


def _pick_tool(user_text: str) -> tuple[str, dict[str, Any]] | None:
    text = user_text.lower()
    for kw, tool, args in _TOOL_KEYWORDS:
        if kw in text:
            return tool, args
    return None


def _id() -> str:
    return f"chatcmpl-mock-{uuid.uuid4().hex[:12]}"


def _now() -> int:
    return int(time.time())


def mock_completion(
    messages: list[dict[str, Any]],
    model: str,
    tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Return an OpenAI-compatible response dict."""

    # Has a tool call already been answered? Look for a ``role: tool`` message.
    has_tool_result = any(m.get("role") == "tool" for m in messages)

    # Most recent user message text.
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text = m.get("content", "") or ""
            break

    if not has_tool_result and tools:
        picked = _pick_tool(user_text)
        if picked is not None:
            tool_name, tool_args = picked
            # Only emit the call if the tool was actually declared in `tools`.
            declared = {
                t.get("function", {}).get("name") for t in tools if t.get("type") == "function"
            }
            if tool_name in declared:
                return _tool_call_response(model, tool_name, tool_args)

    if has_tool_result:
        # Build a short summary that paraphrases the shaped tool response.
        last_tool = next(m for m in reversed(messages) if m.get("role") == "tool")
        body = last_tool.get("content", "")
        summary = _summarise(body, user_text)
        return _text_response(model, summary)

    # Default: a generic apology so the API contract still holds.
    return _text_response(
        model,
        "I don't have a tool to answer that yet. (mock-mode response)",
    )


def _tool_call_response(model: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _text_response(model: str, text: str) -> dict[str, Any]:
    return {
        "id": _id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _summarise(tool_response_json: str, user_text: str) -> str:
    """Build a plausible reply that quotes the tool result."""
    try:
        data = json.loads(tool_response_json)
    except Exception:
        return f"(mock) Tool returned: {tool_response_json[:160]}"

    # Order use-case template.
    if isinstance(data, dict) and "order_id" in data:
        status = data.get("status", "unknown")
        tracking = data.get("tracking_number")
        eta = data.get("estimated_delivery")
        carrier = data.get("carrier")
        bits = [f"Order #{data['order_id']} is currently '{status}'."]
        if carrier and tracking:
            bits.append(f"It's being shipped by {carrier} (tracking: {tracking}).")
        if eta:
            bits.append(f"Estimated delivery: {eta}.")
        bits.append("Let me know if you need me to file a delay claim.")
        return " ".join(bits)

    # Lead use-case template.
    if isinstance(data, dict) and ("FirstName" in data or "LastName" in data):
        name = " ".join(filter(None, [data.get("FirstName"), data.get("LastName")]))
        company = data.get("Company", "their company")
        status = data.get("Status", "unknown")
        return f"{name} at {company} is currently in '{status}' status."

    return f"(mock) Tool returned: {json.dumps(data)[:200]}"


__all__ = ["mock_completion"]
