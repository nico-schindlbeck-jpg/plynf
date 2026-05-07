# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Per-tool cost estimates in USD.

For v0.1 we keep a hardcoded table. Cached calls cost zero (the gateway saved
the call) — the caller logic in ``cache.py``/``api.py`` is responsible for
zeroing the bill, this module simply quotes the *would-be* per-call cost.
"""

from __future__ import annotations

DEFAULT_COST_USD: float = 0.0001

PER_TOOL_COST_USD: dict[str, float] = {
    "web.fetch": 0.0005,
    "web.search": 0.001,
    "fs.read": 0.00005,
    "fs.write": 0.00005,
    "notes.add": 0.00001,
    "notes.list": 0.00001,
}


def estimate_cost(tool_id: str, *, cached: bool = False) -> float:
    """Return the cost in USD for a single invocation.

    Args:
        tool_id: registered tool id.
        cached:  whether the call was served from cache (cost = 0).

    Returns:
        cost in USD as ``float``.
    """
    if cached:
        return 0.0
    return PER_TOOL_COST_USD.get(tool_id, DEFAULT_COST_USD)
