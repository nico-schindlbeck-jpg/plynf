# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Per-tenant policy overrides — live editing from the dashboard.

The shipped YAML files in ``src/plinth_proxy/policies/`` define a *system
default* policy per connector. A tenant can override any rule (allow_fields,
deny_fields, cache_ttl, etc.) through this module without touching the
file system. Overrides are persisted to a JSON file on disk and reloaded
on process start; production swaps the file for the Postgres sink.

The store is intentionally simple — a flat dict keyed by
``{tenant_id}::{connector}::{tool}`` -> partial-policy-dict. The dashboard's
PUT request sends only the fields the user actually changed; we merge them
on top of the system default at read time.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock
from typing import Any

from .policy_engine import (
    ConnectorPolicy,
    RedactRule,
    ToolPolicy,
    _parse_redact,
)

log = logging.getLogger("plinth.proxy.overrides")


class PolicyOverrideStore:
    """File-backed key/value store for per-tenant policy diffs."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path else None
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = Lock()
        self._load()

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — corrupt overrides shouldn't kill the proxy
            log.warning("policy-overrides file corrupt, ignoring", exc_info=True)
            self._data = {}

    def _flush(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    def _key(self, tenant_id: str, connector: str, tool: str) -> str:
        return f"{tenant_id}::{connector}::{tool}"

    def get(self, tenant_id: str, connector: str, tool: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._data.get(self._key(tenant_id, connector, tool), {}))

    def all_for_tenant(self, tenant_id: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        prefix = f"{tenant_id}::"
        with self._lock:
            for k, v in self._data.items():
                if k.startswith(prefix):
                    out[k[len(prefix):]] = dict(v)
        return out

    def set(
        self,
        tenant_id: str,
        connector: str,
        tool: str,
        override: dict[str, Any],
    ) -> None:
        with self._lock:
            self._data[self._key(tenant_id, connector, tool)] = dict(override)
            self._flush()

    def clear(self, tenant_id: str, connector: str, tool: str) -> None:
        with self._lock:
            self._data.pop(self._key(tenant_id, connector, tool), None)
            self._flush()


def merge_override(base: ToolPolicy, override: dict[str, Any]) -> ToolPolicy:
    """Return a new ToolPolicy with ``override`` applied on top of ``base``."""
    if not override:
        return base
    allow_fields = (
        tuple(override["allow_fields"])
        if "allow_fields" in override
        else base.allow_fields
    )
    return ToolPolicy(
        tool=base.tool,
        allow_fields=allow_fields,
        deny_fields=tuple(override.get("deny_fields", base.deny_fields)),
        max_response_tokens=override.get("max_response_tokens", base.max_response_tokens),
        strip_metadata=bool(override.get("strip_metadata", base.strip_metadata)),
        cache_ttl=override.get("cache_ttl", base.cache_ttl),
        redact_pii=_parse_override_redact(override.get("redact_pii"), base.redact_pii),
        block_write_actions=bool(
            override.get("block_write_actions", base.block_write_actions)
        ),
    )


def _parse_override_redact(
    raw: Any, fallback: RedactRule | None
) -> RedactRule | None:
    if raw is None:
        return fallback
    return _parse_redact(raw)


def policy_to_dict(connector: str, tool: ToolPolicy) -> dict[str, Any]:
    """Render a :class:`ToolPolicy` as a JSON-friendly dict (for the UI)."""
    d: dict[str, Any] = {
        "connector": connector,
        "tool": tool.tool,
        "strip_metadata": tool.strip_metadata,
        "block_write_actions": tool.block_write_actions,
    }
    if tool.allow_fields is not None:
        d["allow_fields"] = list(tool.allow_fields)
    if tool.deny_fields:
        d["deny_fields"] = list(tool.deny_fields)
    if tool.max_response_tokens is not None:
        d["max_response_tokens"] = tool.max_response_tokens
    if tool.cache_ttl is not None:
        d["cache_ttl"] = tool.cache_ttl
    if tool.redact_pii is not None:
        d["redact_pii"] = {
            "fields": list(tool.redact_pii.fields),
            "mode": tool.redact_pii.mode,
        }
    return d


def effective_policies_for_tenant(
    policies: dict[str, ConnectorPolicy],
    overrides: PolicyOverrideStore,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Return the dashboard payload: one entry per tool with effective policy."""
    out: list[dict[str, Any]] = []
    for conn_name, cp in policies.items():
        for tool_name, base in cp.tools.items():
            override = overrides.get(tenant_id, conn_name, tool_name)
            effective = merge_override(base, override) if override else base
            entry = policy_to_dict(conn_name, effective)
            entry["has_override"] = bool(override)
            out.append(entry)
    return out


__all__ = [
    "PolicyOverrideStore",
    "effective_policies_for_tenant",
    "merge_override",
    "policy_to_dict",
]
