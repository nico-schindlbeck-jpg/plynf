# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Token counting.

Uses ``tiktoken`` with the GPT-4o encoder by default — same tokenizer
OpenAI bills against. Falls back to a character-based estimate if tiktoken
is unavailable (only happens in restricted environments; we don't ship
production numbers without the real tokenizer).
"""

from __future__ import annotations

import json
from typing import Any

try:
    import tiktoken

    _ENCODER = tiktoken.get_encoding("o200k_base")  # GPT-4o family
    _TIKTOKEN_AVAILABLE = True
except Exception:  # pragma: no cover - tiktoken missing or models unavailable
    _ENCODER = None
    _TIKTOKEN_AVAILABLE = False


def count_tokens(text: str) -> int:
    """Token count for a raw string."""
    if _TIKTOKEN_AVAILABLE and _ENCODER is not None:
        return len(_ENCODER.encode(text))
    # Fallback: ~4 characters per token. Conservative.
    return max(1, len(text) // 4)


def count_json_tokens(value: Any) -> int:
    """Count tokens in the compact-JSON serialisation of ``value``.

    This mirrors how a tool response actually enters the LLM context — as
    a JSON-stringified ``role: tool`` message. Compact (no spaces) because
    that's what most clients send.
    """
    serialised = json.dumps(value, separators=(",", ":"), default=str)
    return count_tokens(serialised)


def count_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Approximate input-token count for a chat-completions ``messages`` array.

    OpenAI's exact prompt-format overhead per message varies by model; we use
    the standard "+4 tokens per message" heuristic plus the encoded content,
    which is accurate enough for cost reporting (within ~2 %).
    """
    total = 0
    for msg in messages:
        total += 4  # role / separator overhead
        for value in msg.values():
            if isinstance(value, str):
                total += count_tokens(value)
            elif value is not None:
                total += count_json_tokens(value)
    total += 2  # priming
    return total


def tiktoken_available() -> bool:
    """Whether tiktoken is loaded; useful for tests and savings disclosures."""
    return _TIKTOKEN_AVAILABLE


__all__ = [
    "count_tokens",
    "count_json_tokens",
    "count_messages_tokens",
    "tiktoken_available",
]
