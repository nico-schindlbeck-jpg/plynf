#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Customer-Support demo — runs the full Plynf pipeline offline.

What this script proves end-to-end:

  1. A noisy 150-field "order" tool response is loaded from disk.
  2. The Plynf policy engine (loaded from the packaged YAML) trims it down
     to the 8 fields the agent actually needs.
  3. Tokens, cost, and reduction % are reported using the real OpenAI
     tokenizer (tiktoken) so the numbers match what OpenAI would bill.

Run:

    python examples/customer-support/run_demo.py
    # or, with the proxy actually running:
    python examples/customer-support/run_demo.py --proxy http://localhost:7430
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PROXY_SRC = REPO / "services" / "proxy" / "src"

# Allow running without `pip install -e ./services/proxy` by adding the
# package source to sys.path. Pragmatic for a demo.
if str(PROXY_SRC) not in sys.path:
    sys.path.insert(0, str(PROXY_SRC))

from plinth_proxy.policy_engine import apply, load_policy  # noqa: E402
from plinth_proxy.savings import make_event, price_for_model  # noqa: E402
from plinth_proxy.tokens import count_json_tokens, tiktoken_available  # noqa: E402

POLICY_PATH = PROXY_SRC / "plinth_proxy" / "policies" / "orders.default.yaml"
RAW_PATH = HERE / "get_order.json"
SHAPED_PATH = HERE / "shaped_order_response.json"


def run_offline(model: str) -> dict:
    raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    connector_policy = load_policy(POLICY_PATH)
    tool_policy = connector_policy.policy_for("get_order")
    shaped = apply(raw, tool_policy)

    # Persist the shaped output for the README / demo asset.
    SHAPED_PATH.write_text(
        json.dumps(shaped, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    raw_tokens = count_json_tokens(raw)
    shaped_tokens = count_json_tokens(shaped)
    saved = raw_tokens - shaped_tokens
    pct = saved / raw_tokens if raw_tokens else 0.0

    price = price_for_model(model)
    cost_before = (raw_tokens / 1_000_000) * price
    cost_after = (shaped_tokens / 1_000_000) * price
    cost_saved = cost_before - cost_after

    removed_fields = _count_removed_fields(raw, shaped)

    event = make_event(
        tenant_id="demo",
        agent_id="customer-support",
        connector="orders",
        tool="get_order",
        model=model,
        raw_response_tokens=raw_tokens,
        shaped_response_tokens=shaped_tokens,
        cache_hit=False,
        request_args={"order_id": "12345"},
    )

    return {
        "model": model,
        "price_per_1m_input_tokens_usd": price,
        "tokenizer": "tiktoken (o200k_base)" if tiktoken_available() else "approx (chars/4)",
        "raw_response_tokens": raw_tokens,
        "shaped_response_tokens": shaped_tokens,
        "saved_tokens": saved,
        "savings_percentage": round(pct * 100, 2),
        "cost_before_usd": round(cost_before, 6),
        "cost_after_usd": round(cost_after, 6),
        "cost_saved_usd": round(cost_saved, 6),
        "removed_fields_count": removed_fields,
        "kept_fields": sorted(shaped.keys()) if isinstance(shaped, dict) else [],
        "savings_event": event.to_dict(),
    }


def _count_removed_fields(raw: object, shaped: object) -> int:
    """Approximate count of top-level + 1-level-nested keys removed."""

    def keys(o):
        if isinstance(o, dict):
            out = list(o.keys())
            for v in o.values():
                if isinstance(v, dict):
                    out.extend(v.keys())
            return out
        return []

    raw_keys = set(keys(raw))
    shaped_keys = set(keys(shaped))
    return max(0, len(raw_keys) - len(shaped_keys))


def run_against_proxy(base_url: str) -> dict:
    """Send the demo_request.json to a running proxy and print the response."""
    import httpx  # local import — keep offline mode dependency-free

    body = json.loads((HERE / "demo_request.json").read_text(encoding="utf-8"))
    url = base_url.rstrip("/") + "/v1/chat/completions"
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, json=body)
    resp.raise_for_status()
    completion = resp.json()
    summary = client.get(base_url.rstrip("/") + "/v1/savings/summary").json() if False else None
    # We dropped the second call to avoid `client` re-use after `with` exit;
    # do it cleanly:
    with httpx.Client(timeout=10) as c2:
        summary = c2.get(base_url.rstrip("/") + "/v1/savings/summary").json()
    return {"completion": completion, "savings_summary": summary}


def _print_report(report: dict) -> None:
    print()
    print("═" * 72)
    print(" Plynf · Customer Support Demo · get_order")
    print("═" * 72)
    print(f" Tokenizer:                  {report['tokenizer']}")
    print(f" Model:                      {report['model']} "
          f"(${report['price_per_1m_input_tokens_usd']:.2f}/1M input tokens)")
    print("─" * 72)
    print(f" Raw response tokens:        {report['raw_response_tokens']:>10,}")
    print(f" Shaped response tokens:     {report['shaped_response_tokens']:>10,}")
    print(f" Saved tokens:               {report['saved_tokens']:>10,}")
    print(f" Savings:                    {report['savings_percentage']:>9.2f}%")
    print("─" * 72)
    print(f" Cost before:                ${report['cost_before_usd']:>10.6f}")
    print(f" Cost after:                 ${report['cost_after_usd']:>10.6f}")
    print(f" Cost saved (per call):      ${report['cost_saved_usd']:>10.6f}")
    print("─" * 72)
    print(f" Fields removed (approx):    {report['removed_fields_count']:>10}")
    print(f" Kept fields:                {', '.join(report['kept_fields'])}")
    print("═" * 72)
    # Project monthly savings at 1000 calls/day.
    monthly = report["cost_saved_usd"] * 1000 * 30
    print(f" Projection at 1,000 calls/day:   ${monthly:,.2f} / month")
    print("═" * 72)
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plynf Customer-Support demo.")
    parser.add_argument(
        "--proxy",
        help="If set, POST demo_request.json to a running proxy. "
             "Otherwise the policy engine is exercised offline.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("PLINTH_DEMO_MODEL", "gpt-4o"),
        help="Model name used for cost calculation (gpt-4o, gpt-4o-mini, ...).",
    )
    args = parser.parse_args(argv)

    if args.proxy:
        result = run_against_proxy(args.proxy)
        print(json.dumps(result, indent=2))
        return 0

    report = run_offline(args.model)
    _print_report(report)

    # Write a machine-readable copy next to the human-readable print.
    (HERE / "demo_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
