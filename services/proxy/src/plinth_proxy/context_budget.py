# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Context budget manager.

For long-running tool-call loops, the message history can balloon past the
LLM's effective input window — at which point you start paying for tokens
the model can't even attend to. This module enforces a configurable hard
budget by rotating out the *oldest* tool-response messages and replacing
them with compact placeholders.

What is protected:

* The system prompt (first message if role=system) — always kept intact.
* The most recent user message — keeps the question the agent is solving.
* The most recent ``keep_recent_tool_messages`` tool messages — they're
  likely still relevant to the current reasoning.
* All assistant messages — rewriting LLM-emitted text breaks coherence.

What gets rotated:

* Older ``role: tool`` messages, oldest-first. Each retired message is
  replaced with a one-line summary so the assistant's prior
  ``tool_call_id`` references still resolve. This keeps the OpenAI schema
  invariant (every assistant tool_call has a matching tool response).
"""

from __future__ import annotations

import json
from typing import Any

from .tokens import count_messages_tokens, count_tokens


def _summarise_tool_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Replace a tool message's content with a short marker."""
    raw_content = msg.get("content")
    serialised = raw_content if isinstance(raw_content, str) else json.dumps(raw_content)
    n_tokens = count_tokens(serialised)
    summary = {
        "_plynf_summarised": True,
        "_original_tokens": n_tokens,
        "_message": f"[tool result retired by context-budget; was ~{n_tokens} tokens]",
    }
    return {**msg, "content": json.dumps(summary)}


def enforce_budget(
    messages: list[dict[str, Any]],
    *,
    max_input_tokens: int,
    keep_recent_tool_messages: int = 3,
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(new_messages, tokens_dropped)`` — possibly equal to input.

    No-op when the message history already fits. When over budget, rotates
    the oldest tool messages first, stopping as soon as the budget is met
    (or we've exhausted the rotateable set).
    """
    current = count_messages_tokens(messages)
    if current <= max_input_tokens:
        return messages, 0

    # Identify indices of tool messages eligible for rotation. We keep the
    # last ``keep_recent_tool_messages`` of them and protect the rest of the
    # rules (system / latest user / assistant).
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    eligible = tool_indices[: max(0, len(tool_indices) - keep_recent_tool_messages)]

    if not eligible:
        # Nothing we're allowed to compress. The pipeline must let the LLM
        # call go through and hope for the best — context_budget never
        # silently drops user messages.
        return messages, 0

    out = list(messages)
    tokens_dropped = 0
    for i in eligible:
        before = count_messages_tokens(out)
        if before <= max_input_tokens:
            break
        # Replace with summary placeholder.
        out[i] = _summarise_tool_message(out[i])
        after = count_messages_tokens(out)
        tokens_dropped += max(0, before - after)
        # Defensive: don't loop forever if a single rotation freed no budget.
        if after >= before:
            break
        # If we now fit, stop.
        if after <= max_input_tokens:
            break

    return out, tokens_dropped


__all__ = ["enforce_budget"]
