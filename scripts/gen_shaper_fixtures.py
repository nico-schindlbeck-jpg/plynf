#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Generate parity fixtures for the client-side shaper port.

Runs the REAL policy engine (`services/proxy/src/plinth_proxy/policy_engine.py`)
and the REAL token counter over the landing-page preview samples plus a set of
edge cases, and writes the expected outputs to
`landing/tests/fixtures/shaper-parity.json`.

`cd landing && npm run test:parity` then asserts the TypeScript port
(`landing/src/lib/shaper.ts`) produces byte-identical shapes and token counts.

Re-run this script whenever policy_engine.py's lossless layer or projection
behaviour changes:

    python scripts/gen_shaper_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "services" / "proxy" / "src"))

from plinth_proxy.policy_engine import ToolPolicy, apply  # noqa: E402
from plinth_proxy.tokens import count_json_tokens, tiktoken_available  # noqa: E402

SAMPLES_PATH = REPO / "landing" / "src" / "lib" / "preview-samples.json"
OUT_PATH = REPO / "landing" / "tests" / "fixtures" / "shaper-parity.json"


def case(name: str, raw, keep_list: list[str] | None) -> dict:
    """Run the real engine with the preview pipeline settings."""
    policy = ToolPolicy(
        tool=name,
        allow_fields=tuple(keep_list) if keep_list else None,
        strip_metadata=True,
        drop_empty_fields=True,
    )
    shaped = apply(raw, policy)
    return {
        "name": name,
        "raw": raw,
        "keepList": keep_list or [],
        "expectedShaped": shaped,
        "expectedRawTokens": count_json_tokens(raw),
        "expectedShapedTokens": count_json_tokens(shaped),
    }


def main() -> None:
    if not tiktoken_available():
        sys.exit("tiktoken is required to generate fixtures (pip install tiktoken)")

    samples = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))

    cases = []
    for s in samples:
        # With the shipped keep-list AND lossless-only — both paths the
        # preview exposes.
        cases.append(case(f"{s['id']}::keep-list", s["raw"], s["keepList"]))
        cases.append(case(f"{s['id']}::lossless-only", s["raw"], None))

    # ── Edge cases ────────────────────────────────────────────────
    cases.append(
        case(
            "edge::zero-false-kept",
            {"count": 0, "ratio": 0.5, "active": False, "note": None, "tags": [], "meta": {}},
            None,
        )
    )
    cases.append(
        case(
            "edge::unicode-ensure-ascii",
            {"customer": "Müller & Söhne — Lieferung", "city": "Zürich", "emoji": "🚀", "ok": True},
            None,
        )
    )
    cases.append(
        case(
            "edge::dotted-paths-and-synonyms",
            {
                "order": {"id": "A1", "shipping": {"carrier": "DHL", "cost": 4.9}},
                "orderNo": "A1-LEGACY",
                "noise": "x",
            },
            ["order.id", "order.shipping.carrier", "order_id|orderno"],
        )
    )
    cases.append(
        case(
            "edge::schema-mismatch-fallback",
            {"completely": "different", "schema": {"than": "expected"}, "createdAt": "2026-01-01"},
            ["order_id", "status", "tracking_number"],
        )
    )
    cases.append(
        case(
            "edge::list-of-dicts-projection",
            {
                "records": [
                    {"id": 1, "name": "a", "internal": "x", "blob": None},
                    {"id": 2, "name": "b", "internal": "y", "blob": ""},
                ],
                "cursor": "abc",
            },
            ["records.id", "records.name"],
        )
    )
    cases.append(
        case(
            "edge::casing-separator-robust",
            {"ORDER-ID": "X9", "Customer_Name": "Acme", "TrackingNumber": "T1", "junk": 1},
            ["order_id", "customer_name", "tracking_number"],
        )
    )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps({"generatedBy": "scripts/gen_shaper_fixtures.py", "cases": cases}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(cases)} cases → {OUT_PATH.relative_to(REPO)}")


if __name__ == "__main__":
    main()
