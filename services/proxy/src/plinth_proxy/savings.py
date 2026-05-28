# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Savings measurement.

Pragmatic MVP: every tool-call interception emits a ``SavingsEvent`` that
records token deltas and computed cost savings. Events are appended to a
JSONL file (or any callable sink). We do NOT persist tool-response bodies —
only counts, hashes, and metadata. The cost calculation is model-aware and
configurable.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Default input-token prices (USD per 1M tokens) — May 2026 snapshot.
# Tool-response tokens enter the LLM as INPUT tokens, so we only quote input prices.
DEFAULT_MODEL_PRICES: dict[str, float] = {
    "gpt-4o": 5.00,
    "gpt-4o-mini": 0.60,
    "gpt-4-turbo": 10.00,
    "gpt-3.5-turbo": 0.50,
    "o1": 15.00,
    "o1-mini": 3.00,
    "claude-3-5-sonnet": 3.00,
    "claude-3-5-haiku": 0.80,
    "claude-3-opus": 15.00,
}


def price_for_model(model: str) -> float:
    """USD per 1M input tokens for ``model``. Falls back to gpt-4o ($5)."""
    # Allow override via env (PLINTH_MODEL_PRICE_GPT_4O=4.5 etc.)
    env_key = "PLINTH_MODEL_PRICE_" + model.upper().replace("-", "_").replace(".", "_")
    env_val = os.environ.get(env_key)
    if env_val is not None:
        try:
            return float(env_val)
        except ValueError:
            pass
    return DEFAULT_MODEL_PRICES.get(model, DEFAULT_MODEL_PRICES["gpt-4o"])


@dataclass
class SavingsEvent:
    """One tool-call interception. JSON-serialisable."""

    ts: float
    tenant_id: str
    agent_id: str | None
    connector: str
    tool: str
    model: str
    raw_response_tokens: int
    shaped_response_tokens: int
    cache_hit: bool
    # Hash of the request args so we can correlate without storing bodies.
    request_hash: str
    # Optional context for richer dashboards.
    workflow_id: str | None = None

    @property
    def saved_tokens(self) -> int:
        if self.cache_hit:
            # Cache hit means we didn't fetch the tool at all; the alternative
            # cost would have been a full raw response. Count the whole thing.
            return self.raw_response_tokens
        return max(0, self.raw_response_tokens - self.shaped_response_tokens)

    @property
    def savings_pct(self) -> float:
        if self.raw_response_tokens == 0:
            return 0.0
        return self.saved_tokens / self.raw_response_tokens

    def cost_saved_usd(self, price_per_1m: float | None = None) -> float:
        price = price_per_1m if price_per_1m is not None else price_for_model(self.model)
        return (self.saved_tokens / 1_000_000) * price

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["saved_tokens"] = self.saved_tokens
        d["savings_pct"] = round(self.savings_pct, 4)
        d["cost_saved_usd"] = round(self.cost_saved_usd(), 6)
        return d


@dataclass
class SavingsSink:
    """Append-only JSONL sink. Thread-safe enough for single-process MVP."""

    path: Path
    _fh: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: SavingsEvent) -> None:
        line = json.dumps(event.to_dict(), separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def hash_request(args: dict[str, Any]) -> str:
    """Deterministic short hash of tool-call arguments."""
    serialised = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(serialised.encode("utf-8")).hexdigest()[:16]


def make_event(
    *,
    tenant_id: str,
    agent_id: str | None,
    connector: str,
    tool: str,
    model: str,
    raw_response_tokens: int,
    shaped_response_tokens: int,
    cache_hit: bool,
    request_args: dict[str, Any],
    workflow_id: str | None = None,
) -> SavingsEvent:
    return SavingsEvent(
        ts=time.time(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        connector=connector,
        tool=tool,
        model=model,
        raw_response_tokens=raw_response_tokens,
        shaped_response_tokens=shaped_response_tokens,
        cache_hit=cache_hit,
        request_hash=hash_request(request_args),
        workflow_id=workflow_id,
    )


# In-process aggregate for the demo dashboard view.
def aggregate(events: list[SavingsEvent]) -> dict[str, Any]:
    """Return a small dashboard-style summary of events."""
    total_raw = sum(e.raw_response_tokens for e in events)
    total_shaped = sum(e.shaped_response_tokens for e in events)
    total_saved = sum(e.saved_tokens for e in events)
    total_cost_saved = sum(e.cost_saved_usd() for e in events)
    cache_hits = sum(1 for e in events if e.cache_hit)
    by_connector: dict[str, int] = {}
    for e in events:
        by_connector[e.connector] = by_connector.get(e.connector, 0) + e.saved_tokens
    return {
        "total_calls": len(events),
        "total_raw_tokens": total_raw,
        "total_shaped_tokens": total_shaped,
        "total_saved_tokens": total_saved,
        "savings_pct": round(total_saved / total_raw, 4) if total_raw else 0.0,
        "total_cost_saved_usd": round(total_cost_saved, 4),
        "cache_hit_rate": round(cache_hits / len(events), 4) if events else 0.0,
        "top_connectors_by_savings": sorted(
            by_connector.items(), key=lambda kv: kv[1], reverse=True
        ),
    }


__all__ = [
    "DEFAULT_MODEL_PRICES",
    "SavingsEvent",
    "SavingsSink",
    "aggregate",
    "hash_request",
    "make_event",
    "price_for_model",
]
