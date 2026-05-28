# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Policy Engine v1 — shape tool responses before they enter the LLM context.

A policy is a YAML document describing, per tool, which fields the LLM is
allowed to see, what should be masked, and how long the result may be cached.

Supported rule types (MVP):

* ``allow_fields``  — whitelist of fields (supports dotted paths for nested)
* ``deny_fields``   — explicit blacklist applied after allow_fields
* ``max_response_tokens`` — hard cap; oversized lists/strings get truncated
* ``strip_metadata`` — drops common audit columns (``created_at`` etc.)
* ``cache_ttl``     — seconds; consumed by the cache layer, not this module
* ``redact_pii``    — hash / mask / remove on named fields
* ``block_write_actions`` — if true and tool name matches a write-pattern
  (``create_``, ``update_``, ``delete_``, ``send_``), the call is rejected

The engine is intentionally pure: ``apply()`` takes a JSON-serialisable value
and returns a JSON-serialisable value. No I/O, no globals.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Default metadata fields stripped by ``strip_metadata: true``.
_DEFAULT_METADATA_FIELDS = frozenset(
    {
        "created_at",
        "createdAt",
        "CreatedDate",
        "created_by",
        "createdBy",
        "updated_at",
        "updatedAt",
        "LastModifiedDate",
        "modified_at",
        "modifiedAt",
        "modified_by",
        "modifiedBy",
        "SystemModstamp",
        "LastViewedDate",
        "LastReferencedDate",
        "etag",
        "_version",
        "version",
        "rev",
        "_rev",
        "attributes",  # Salesforce wrapper
    }
)

# Tool names matching these prefixes are treated as write actions.
_WRITE_PREFIXES = ("create_", "update_", "delete_", "send_", "post_", "patch_", "put_")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RedactRule:
    """How to redact a PII field."""

    fields: tuple[str, ...]
    mode: str = "hash"  # "hash" | "mask" | "remove"

    def apply_to(self, value: Any) -> Any:
        if value is None:
            return value
        if self.mode == "remove":
            return None
        if self.mode == "mask":
            return "***"
        # hash mode (default): first 8 chars of sha256, prefixed for clarity
        digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]
        return f"sha256:{digest}"


@dataclass
class ToolPolicy:
    """Compiled policy for a single tool."""

    tool: str
    allow_fields: tuple[str, ...] | None = None  # None = no whitelist
    deny_fields: tuple[str, ...] = ()
    max_response_tokens: int | None = None
    strip_metadata: bool = False
    cache_ttl: int | None = None  # consumed elsewhere
    redact_pii: RedactRule | None = None
    block_write_actions: bool = False

    @property
    def is_write_action(self) -> bool:
        return any(self.tool.startswith(p) for p in _WRITE_PREFIXES)


@dataclass
class ConnectorPolicy:
    """All tool policies for one connector (Salesforce, Slack, ...)."""

    connector: str
    version: int = 1
    tools: dict[str, ToolPolicy] = field(default_factory=dict)
    defaults: ToolPolicy | None = None

    def policy_for(self, tool_name: str) -> ToolPolicy:
        """Return the effective policy for a tool, merging defaults."""
        if tool_name in self.tools:
            return self.tools[tool_name]
        # Fall back to defaults (which may also be None — return a no-op).
        if self.defaults is not None:
            # Defaults carry connector-wide settings but no field allow-list,
            # so calling tools fall through and return the full response.
            return ToolPolicy(
                tool=tool_name,
                allow_fields=None,
                deny_fields=self.defaults.deny_fields,
                max_response_tokens=self.defaults.max_response_tokens,
                strip_metadata=self.defaults.strip_metadata,
                cache_ttl=self.defaults.cache_ttl,
                redact_pii=self.defaults.redact_pii,
                block_write_actions=self.defaults.block_write_actions,
            )
        return ToolPolicy(tool=tool_name)


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def _parse_redact(raw: Any) -> RedactRule | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"redact_pii must be a mapping, got {type(raw).__name__}")
    fields = tuple(raw.get("fields") or ())
    mode = raw.get("mode", "hash")
    if mode not in {"hash", "mask", "remove"}:
        raise ValueError(f"redact_pii.mode must be hash|mask|remove, got {mode!r}")
    return RedactRule(fields=fields, mode=mode)


def _parse_tool_policy(name: str, raw: dict[str, Any]) -> ToolPolicy:
    return ToolPolicy(
        tool=name,
        allow_fields=tuple(raw["allow_fields"]) if "allow_fields" in raw else None,
        deny_fields=tuple(raw.get("deny_fields") or ()),
        max_response_tokens=raw.get("max_response_tokens"),
        strip_metadata=bool(raw.get("strip_metadata", False)),
        cache_ttl=raw.get("cache_ttl"),
        redact_pii=_parse_redact(raw.get("redact_pii")),
        block_write_actions=bool(raw.get("block_write_actions", False)),
    )


def load_policy(path: str | Path) -> ConnectorPolicy:
    """Load a YAML connector policy from disk."""
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top-level YAML must be a mapping")

    connector = raw.get("connector") or p.stem.split(".")[0]
    version = int(raw.get("version", 1))
    defaults_raw = raw.get("defaults") or {}
    tools_raw = raw.get("tools") or {}

    defaults = _parse_tool_policy("__defaults__", defaults_raw) if defaults_raw else None
    tools: dict[str, ToolPolicy] = {}
    for tool_name, tool_raw in tools_raw.items():
        merged = _merge_with_defaults(tool_raw, defaults_raw)
        tools[tool_name] = _parse_tool_policy(tool_name, merged)

    return ConnectorPolicy(
        connector=connector, version=version, tools=tools, defaults=defaults
    )


def _merge_with_defaults(tool_raw: dict[str, Any], defaults_raw: dict[str, Any]) -> dict[str, Any]:
    """Merge connector defaults into per-tool config; tool-level wins."""
    merged = dict(defaults_raw)
    merged.update(tool_raw)
    return merged


def load_all_policies(directory: str | Path) -> dict[str, ConnectorPolicy]:
    """Load every ``*.yaml`` file in ``directory`` into a registry by connector name."""
    d = Path(directory)
    out: dict[str, ConnectorPolicy] = {}
    for p in sorted(d.glob("*.yaml")):
        policy = load_policy(p)
        out[policy.connector] = policy
    return out


# ---------------------------------------------------------------------------
# Core: apply()
# ---------------------------------------------------------------------------


class PolicyError(Exception):
    """Raised when a write-action is blocked by policy."""


def apply(
    response: Any,
    policy: ToolPolicy,
    *,
    token_counter: callable | None = None,
) -> Any:
    """Return a reshaped copy of ``response`` according to ``policy``.

    ``response`` is any JSON-serialisable value. ``token_counter`` is an
    optional callable ``(str) -> int`` used to enforce ``max_response_tokens``;
    pass :func:`plinth_proxy.tokens.count_tokens` in production.
    """

    if policy.block_write_actions and policy.is_write_action:
        raise PolicyError(f"write action blocked by policy: {policy.tool}")

    out = response

    if policy.strip_metadata:
        out = _strip_metadata(out)

    if policy.allow_fields:
        out = _project_fields(out, policy.allow_fields)

    if policy.deny_fields:
        out = _remove_fields(out, policy.deny_fields)

    if policy.redact_pii:
        out = _redact_pii(out, policy.redact_pii)

    if policy.max_response_tokens is not None:
        out = _truncate_to_tokens(out, policy.max_response_tokens, token_counter)

    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _strip_metadata(v)
            for k, v in value.items()
            if k not in _DEFAULT_METADATA_FIELDS
        }
    if isinstance(value, list):
        return [_strip_metadata(item) for item in value]
    return value


def _split_path(path: str) -> list[str]:
    return path.split(".") if "." in path else [path]


def _is_prefix(field_path: list[str], allow_path: list[str]) -> bool:
    """Return True if ``allow_path`` is a prefix of ``field_path``."""
    if len(allow_path) > len(field_path):
        return False
    return field_path[: len(allow_path)] == allow_path


def _project_fields(value: Any, allow: tuple[str, ...]) -> Any:
    """Whitelist projection. Supports dotted paths for nested objects."""
    if not isinstance(value, dict):
        # Lists: project each element.
        if isinstance(value, list):
            return [_project_fields(item, allow) for item in value]
        return value

    # Build a trie of allow paths for efficient lookup.
    allow_paths = [_split_path(a) for a in allow]
    return _project_dict(value, allow_paths, current_path=[])


def _project_dict(d: dict, allow_paths: list[list[str]], current_path: list[str]) -> dict:
    out: dict[str, Any] = {}
    for key, val in d.items():
        new_path = [*current_path, key]
        # Is this key allowed at the top level OR inside a deeper path?
        exact_match = any(new_path == ap for ap in allow_paths)
        nested_match = any(_is_prefix(ap, new_path) for ap in allow_paths)
        descends_into = any(_is_prefix(new_path, ap[:-1]) for ap in allow_paths if len(ap) > 1)

        if exact_match:
            out[key] = val
        elif descends_into and isinstance(val, dict):
            sub = _project_dict(val, allow_paths, new_path)
            if sub:
                out[key] = sub
        elif descends_into and isinstance(val, list):
            sub_list = [
                _project_dict(item, allow_paths, new_path) if isinstance(item, dict) else item
                for item in val
            ]
            # Filter out empty dicts so we don't pad the response with {}.
            sub_list = [s for s in sub_list if not (isinstance(s, dict) and not s)]
            if sub_list:
                out[key] = sub_list
        elif nested_match:
            # Allow path is deeper than this key; include the whole subtree.
            out[key] = val
    return out


def _remove_fields(value: Any, deny: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        deny_set_top = {d for d in deny if "." not in d}
        deny_nested = [d for d in deny if "." in d]
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in deny_set_top:
                continue
            # Apply nested removals.
            sub_deny = [d.split(".", 1)[1] for d in deny_nested if d.split(".", 1)[0] == k]
            if sub_deny:
                out[k] = _remove_fields(v, tuple(sub_deny))
            else:
                out[k] = _remove_fields(v, deny)
        return out
    if isinstance(value, list):
        return [_remove_fields(item, deny) for item in value]
    return value


def _redact_pii(value: Any, rule: RedactRule) -> Any:
    target_top = {f for f in rule.fields if "." not in f}
    target_nested = [f for f in rule.fields if "." in f]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in target_top:
                out[k] = rule.apply_to(v)
                continue
            sub_target = [t.split(".", 1)[1] for t in target_nested if t.split(".", 1)[0] == k]
            if sub_target:
                out[k] = _redact_pii(v, RedactRule(fields=tuple(sub_target), mode=rule.mode))
            else:
                out[k] = _redact_pii(v, rule)
        # Remove keys whose value became None via "remove" mode.
        if rule.mode == "remove":
            out = {k: v for k, v in out.items() if v is not None}
        return out
    if isinstance(value, list):
        return [_redact_pii(item, rule) for item in value]
    return value


def _truncate_to_tokens(
    value: Any, max_tokens: int, token_counter: callable | None
) -> Any:
    """Truncate the JSON serialisation to fit a token budget.

    For lists, we drop trailing elements until under budget. For strings, we
    cut characters. For dicts, we drop trailing keys. Pragmatic, not optimal.
    """
    counter = token_counter or _len_approx

    serialised = json.dumps(value, separators=(",", ":"))
    if counter(serialised) <= max_tokens:
        return value

    if isinstance(value, list):
        # Reserve a few tokens for the truncation marker so we always include it.
        marker = {"_plynf_truncated": True, "_dropped_items": 0}
        marker_tokens = counter(json.dumps(marker, separators=(",", ":")))
        budget = max(0, max_tokens - marker_tokens)

        out_list = list(value)
        while out_list and counter(json.dumps(out_list, separators=(",", ":"))) > budget:
            out_list.pop()
        marker["_dropped_items"] = len(value) - len(out_list)
        out_list.append(marker)
        return out_list

    if isinstance(value, dict):
        items = list(value.items())
        while items and counter(json.dumps(dict(items), separators=(",", ":"))) > max_tokens:
            items.pop()
        out_dict = dict(items)
        if len(items) < len(value):
            out_dict["_plynf_truncated"] = True
        return out_dict

    if isinstance(value, str):
        # Binary-search a character cut so we land just under max_tokens.
        lo, hi = 0, len(value)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if counter(value[:mid]) <= max_tokens:
                lo = mid
            else:
                hi = mid - 1
        return value[:lo] + "…[truncated]"

    return value


def _len_approx(s: str) -> int:
    """Cheap fallback when tiktoken isn't loaded — ~4 chars per token."""
    return max(1, len(s) // 4)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "ConnectorPolicy",
    "PolicyError",
    "RedactRule",
    "ToolPolicy",
    "apply",
    "load_all_policies",
    "load_policy",
]
