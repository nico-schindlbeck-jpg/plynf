# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Offline token counting and cost estimation helpers.

Plinth intentionally avoids round-tripping to a model just to count
tokens. We use ``tiktoken`` with the ``cl100k_base`` encoding, which is
a close-enough approximation for Anthropic's BPE tokenizer for the
budgeting and observability use cases the SDK targets.

The encoding is loaded lazily and cached at module level so repeated
``count`` calls do not pay the constructor cost.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    import tiktoken


# ---------------------------------------------------------------------------
# Pricing constants — keep these obvious so they're easy to refresh.
# ---------------------------------------------------------------------------

#: Anthropic Claude Sonnet input pricing in USD per 1M tokens.
SONNET_INPUT_USD_PER_MTOK: float = 3.0

#: Anthropic Claude Sonnet output pricing in USD per 1M tokens.
SONNET_OUTPUT_USD_PER_MTOK: float = 15.0

#: The tiktoken encoding name we standardise on. ``cl100k_base`` is the
#: encoding used by recent OpenAI models and is the closest publicly
#: available BPE to Anthropic's tokenizer.
ENCODING_NAME: str = "cl100k_base"


@lru_cache(maxsize=1)
def _get_encoding() -> tiktoken.Encoding:
    """Load and cache the tiktoken encoding.

    We use ``functools.lru_cache`` rather than a module-level global so
    that test code can clear the cache in fixtures without monkey-
    patching internals.
    """
    import tiktoken  # local import keeps SDK import time low

    return tiktoken.get_encoding(ENCODING_NAME)


def count(text: str) -> int:
    """Return the number of tokens in ``text``.

    Args:
        text: Any string. Empty strings return ``0``.

    Returns:
        The number of tokens produced by the ``cl100k_base`` encoding.
    """
    if not text:
        return 0
    encoding = _get_encoding()
    return len(encoding.encode(text))


def estimate_cost(prompt_tokens: int, completion_tokens: int = 0) -> float:
    """Estimate the USD cost of a Sonnet request.

    Args:
        prompt_tokens: Number of input/prompt tokens.
        completion_tokens: Number of generated tokens. Defaults to 0 so
            callers can estimate prompt-only cost.

    Returns:
        The estimated cost in USD, computed at Sonnet pricing.
    """
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("Token counts must be non-negative")
    input_cost = prompt_tokens * SONNET_INPUT_USD_PER_MTOK / 1_000_000
    output_cost = completion_tokens * SONNET_OUTPUT_USD_PER_MTOK / 1_000_000
    return input_cost + output_cost


__all__ = [
    "ENCODING_NAME",
    "SONNET_INPUT_USD_PER_MTOK",
    "SONNET_OUTPUT_USD_PER_MTOK",
    "count",
    "estimate_cost",
]
