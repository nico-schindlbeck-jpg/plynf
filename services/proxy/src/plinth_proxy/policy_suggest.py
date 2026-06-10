# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Auto keep-list suggestion (P1.1, server side).

Given a raw tool response, propose an ``allow_fields`` keep-list an
operator can review — deterministic heuristics, no model call.
**Suggest, never silently auto-apply**: this module only proposes; the
operator applies via the dashboard policy editor (or copies the YAML).

This is the server-side counterpart of the public preview's
``landing/src/lib/suggest.ts``. Keep the two heuristics in sync — same
three tiers:

1. hard-drop — audit metadata, internal/sync plumbing, opaque refs,
   hash-/token-like values, API URLs, foreign-key ids
2. keep      — business vocabulary (status, amount, name, dates …) and
   short human-readable strings
3. fallback  — if the suggestion would keep almost nothing, keep all
   short scalar fields instead (never suggest a blanking policy)

The endpoint wrapping this (``POST /v1/suggest-policy``) is NOT in the
request hot path — shaping stays a pure transform.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .policy_engine import _DEFAULT_METADATA_NORM, _norm

# Business vocabulary — a normalised field name CONTAINING one of these is
# very likely the answer to an agent's question. (Mirror of suggest.ts.)
_KEEP_TOKENS = (
    "status", "state", "stage", "name", "title", "company", "amount",
    "total", "price", "revenue", "quantity", "qty", "count", "summary",
    "subject", "text", "message", "description", "carrier", "tracking",
    "delivery", "due", "closedate", "date", "rating", "probability",
    "nextstep", "tier", "industry", "employees", "website", "city",
    "country", "currency", "sku", "user", "owner", "type",
)

# Plumbing vocabulary — drop regardless of value. (Mirror of suggest.ts.)
_DROP_TOKENS = (
    "internal", "sync", "legacy", "fingerprint", "token", "secret",
    "checksum", "etag", "guid", "uuid", "cursor", "pushcount", "fiscal",
    "jigsaw", "duns", "naics", "sic", "geocode", "latitude", "longitude",
    "photourl", "clientmsgid", "blockid", "recordtype", "pricebook",
    "masterrecord", "historyid", "audittrail", "auditlog", "metadata",
    "warehousebin", "taxcode", "taxbreakdown", "routingcode", "processorref",
    "supplierref", "fraudscore", "fraudvendor", "marketingsegments",
    "marketingconsent", "campaigntouchpoints", "deletedflag", "isdeleted",
)

_HASH_RE = re.compile(r"^[A-Za-z0-9_+=-]{18,}$")
_TWO_DIGITS_RE = re.compile(r"\d.*\d")
_READABLE_RE = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .,&-]*$")


def _looks_like_id(n: str) -> bool:
    return n == "id" or n.endswith("id")


def _looks_like_hash(v: str) -> bool:
    # Opaque machine string: long, no spaces/slashes, contains digits —
    # "Negotiation/Review" must NOT match.
    return bool(_HASH_RE.match(v)) and bool(_TWO_DIGITS_RE.search(v))


def _looks_like_api_path(v: str) -> bool:
    return v.startswith(("/", "http://", "https://"))


@dataclass
class SuggestedField:
    path: str
    keep: bool
    reason: str


@dataclass
class Suggestion:
    keep_list: list[str] = field(default_factory=list)
    fields: list[SuggestedField] = field(default_factory=list)
    used_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "keep_list": self.keep_list,
            "used_fallback": self.used_fallback,
            "fields": [
                {"path": f.path, "keep": f.keep, "reason": f.reason} for f in self.fields
            ],
        }


def _collect_leaves(
    value: Any, path: tuple[str, ...] = (), out: list | None = None, depth: int = 0
) -> list[tuple[tuple[str, ...], Any]]:
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(value, dict):
        for k, v in value.items():
            _collect_leaves(v, (*path, k), out, depth + 1)
        return out
    if isinstance(value, list):
        # Sample the first element — schema, not data, drives the suggestion.
        if value:
            _collect_leaves(value[0], path, out, depth + 1)
        return out
    out.append((path, value))
    return out


def _classify(path: tuple[str, ...], value: Any, is_first_id: bool) -> SuggestedField:
    dotted = ".".join(path)
    last = _norm(path[-1]) if path else ""
    whole = _norm(dotted)

    if last in _DEFAULT_METADATA_NORM:
        return SuggestedField(dotted, False, "audit metadata")
    if value is None or value == "":
        return SuggestedField(dotted, False, "empty — lossless layer removes it anyway")
    if any(t in whole for t in _DROP_TOKENS):
        return SuggestedField(dotted, False, "internal plumbing")
    if _looks_like_id(last):
        if is_first_id:
            return SuggestedField(dotted, True, "primary identifier")
        return SuggestedField(dotted, False, "foreign-key reference")
    # Name-based intent beats value shape: a field CALLED tracking_number /
    # stage_name is business signal even when its VALUE looks machine-y.
    if any(t in last for t in _KEEP_TOKENS):
        return SuggestedField(dotted, True, "business field")
    if isinstance(value, str) and _looks_like_api_path(value):
        return SuggestedField(dotted, False, "API url/path")
    if isinstance(value, str) and _looks_like_hash(value):
        return SuggestedField(dotted, False, "opaque token/hash")
    if isinstance(value, (int, float, bool)):
        return SuggestedField(dotted, False, "unrecognised scalar — review")
    if isinstance(value, str) and len(value) <= 80 and (
        " " in value or _READABLE_RE.match(value)
    ):
        return SuggestedField(dotted, True, "human-readable value")
    return SuggestedField(dotted, False, "unrecognised — review")


def suggest_keep_list(raw: Any) -> Suggestion:
    """Propose a keep-list for ``raw`` (any JSON-serialisable tool response)."""
    leaves = _collect_leaves(raw)

    first_id_path: str | None = None
    for path, _value in leaves:
        last = _norm(path[-1]) if path else ""
        if _looks_like_id(last) and last not in _DEFAULT_METADATA_NORM:
            whole = _norm(".".join(path))
            if not any(t in whole for t in _DROP_TOKENS):
                first_id_path = ".".join(path)
                break

    seen: set[str] = set()
    fields: list[SuggestedField] = []
    values_by_path: dict[str, Any] = {}
    for path, value in leaves:
        dotted = ".".join(path)
        if dotted in seen:
            continue
        seen.add(dotted)
        values_by_path[dotted] = value
        fields.append(_classify(path, value, dotted == first_id_path))

    keep_list = [f.path for f in fields if f.keep]
    used_fallback = False

    # Never suggest a blanking policy: if (almost) nothing matched, keep all
    # short scalar fields and let the operator trim.
    if len(keep_list) < 3:
        used_fallback = True
        keep_list = [
            f.path
            for f in fields
            if values_by_path.get(f.path) not in (None, "")
            and len(str(values_by_path.get(f.path))) <= 80
        ]

    return Suggestion(keep_list=keep_list, fields=fields, used_fallback=used_fallback)


def render_policy_yaml(tool: str, keep_list: list[str], *, connector: str = "my-connector") -> str:
    """Render a ready-to-use connector policy (mirror of suggest.ts)."""
    safe_tool = tool.strip() or "my_tool"
    lines = [
        "# Plynf connector policy — generated suggestion. Review before shipping.",
        "# Lossless minimisation (strip_metadata + drop_empty_fields) is safe everywhere.",
        f"connector: {connector}",
        "version: 1",
        "",
        "defaults:",
        "  strip_metadata: true",
        "  drop_empty_fields: true",
        "",
        "tools:",
        f"  {safe_tool}:",
        "    allow_fields:",
        *[f"      - {f}" for f in keep_list],
    ]
    return "\n".join(lines) + "\n"


__all__ = ["Suggestion", "SuggestedField", "render_policy_yaml", "suggest_keep_list"]
