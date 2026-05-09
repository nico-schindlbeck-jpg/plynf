# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""GDPR compliance helpers for the gateway service.

The :class:`GatewayComplianceStore` enumerates and hard-deletes every
tenant-scoped row in the gateway database.

Secrets are *always* redacted in the export:

* ``oauth_connections.access_token_encrypted`` and
  ``refresh_token_encrypted`` are returned as the literal string
  ``"REDACTED"`` so the export is GDPR-portable but doesn't leak the
  encryption-wrapped tokens. The encrypted blobs themselves are useless
  without the encryption key, but the export goes to the *user's* hands —
  shipping them is unnecessary and a defence-in-depth no-no.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from .db import Database


_REDACTED = "REDACTED"

# Tables that carry tenant_id directly. Order doesn't matter for export
# but matters for delete (children before parents — none of these have
# foreign-key children, so the order is purely cosmetic).
_TENANT_TABLES: tuple[str, ...] = (
    "audit_events",
    "agent_limits",
    "oauth_connections",
    "oauth_states",
    "tools",
)


def _row_to_jsonl(row: Any, type_: str, *, redact: tuple[str, ...] = ()) -> str:
    """Serialise an aiosqlite Row as a JSONL line tagged with ``type``.

    Columns named in ``redact`` are replaced with ``"REDACTED"``.
    """

    payload: dict[str, Any] = {"type": type_}
    for key in row.keys():
        if key in redact:
            payload[key] = _REDACTED
            continue
        payload[key] = row[key]
    return json.dumps(payload, sort_keys=True, default=str)


class GatewayComplianceStore:
    """Tenant-scoped export + delete operations on the gateway DB."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def export_jsonl(self, tenant_id: str) -> AsyncIterator[str]:
        """Yield JSONL lines for every tenant-scoped row.

        Per-row JSON shape: ``{"type": "<row_type>", ...columns}`` with
        secret-bearing columns redacted to ``"REDACTED"`` strings. Tables
        covered: ``audit_events``, ``agent_limits``, ``oauth_connections``
        (tokens redacted), ``oauth_states``, ``tools``.

        The ``cache_entries`` table is intentionally NOT included — cache
        rows aren't tenant-scoped (cache is keyed only by tool_id +
        argument hash) and contain results derived from tools the tenant
        already owned. The cache is wiped wholesale during the delete
        cascade as a defence-in-depth measure.
        """

        lines = await self._collect_lines(tenant_id)
        for line in lines:
            yield line

    async def _collect_lines(self, tenant_id: str) -> list[str]:
        out: list[str] = []
        for table, type_, redact in _TABLE_SPECS:
            rows = await self._db.fetchall(
                f"SELECT * FROM {table} WHERE tenant_id = ?",
                (tenant_id,),
            )
            for row in rows:
                out.append(_row_to_jsonl(row, type_, redact=redact))
        return out

    async def delete_tenant_data(self, tenant_id: str) -> dict[str, int]:
        """Hard-delete every tenant-scoped row.

        Returns counts per table. The cache is wiped *wholesale* (it isn't
        tenant-keyed but may contain results derived from this tenant's
        tools) — defence-in-depth for the GDPR cascade.
        """

        counts: dict[str, int] = {}
        for table, _, _ in _TABLE_SPECS:
            rows = await self._db.fetchall(
                f"SELECT COUNT(*) AS c FROM {table} WHERE tenant_id = ?",
                (tenant_id,),
            )
            count_before = int(rows[0]["c"]) if rows else 0
            await self._db.execute(
                f"DELETE FROM {table} WHERE tenant_id = ?",
                (tenant_id,),
            )
            counts[table] = count_before

        # Cache wipe — defensive. Cache rows aren't tenant-keyed so we
        # can't be surgical without knowing which results came from which
        # tenant; the SAFE thing under GDPR is to invalidate the lot.
        rows = await self._db.fetchall("SELECT COUNT(*) AS c FROM cache_entries")
        cache_count = int(rows[0]["c"]) if rows else 0
        await self._db.execute("DELETE FROM cache_entries")
        counts["cache_entries"] = cache_count

        return counts


# Per-table export specification: (table, jsonl_type_label, redacted_columns).
_TABLE_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("tools", "tool", ()),
    ("audit_events", "audit_event", ()),
    ("agent_limits", "agent_limits", ()),
    (
        "oauth_connections",
        "oauth_connection",
        ("access_token_encrypted", "refresh_token_encrypted"),
    ),
    ("oauth_states", "oauth_state", ("pkce_verifier",)),
)


__all__ = ["GatewayComplianceStore"]
