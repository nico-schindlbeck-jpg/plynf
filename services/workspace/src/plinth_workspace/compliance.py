# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""GDPR compliance helpers for the workspace service.

The :class:`WorkspaceComplianceStore` knows how to enumerate every
tenant-scoped row in the workspace database (for export) and how to
hard-delete every tenant-scoped row in dependency order (for the GDPR
``erasure`` cascade).

The module is intentionally separate from :mod:`storage` so the cascade
order — which spans many tables, including foreign-key children — is
explicit and easy to audit.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .db import connect


# Tables whose data lives directly under a workspace via the
# ``workspace_id`` column. Order matters for delete: children-of-
# children first, parents last.
_DIRECT_CHILD_TABLES: tuple[str, ...] = (
    "channel_consumers",
    "channel_messages",
    "channel_schemas",
    "channels",
    "kv_entries",
    "file_entries",
    "branches",
    "snapshots",
    "workflows",
    "retention_policies",
    "resource_locks",
)


def _row_to_jsonl(row: Any, type_: str) -> str:
    """Serialise an aiosqlite Row as a JSONL line tagged with ``type``."""

    payload: dict[str, Any] = {"type": type_}
    for key in row.keys():
        value = row[key]
        # aiosqlite types are mostly already JSON-serialisable.
        payload[key] = value
    return json.dumps(payload, sort_keys=True, default=str)


class WorkspaceComplianceStore:
    """Tenant-scoped export + delete operations."""

    def __init__(self, db_path: Path, blobs_dir: Path) -> None:
        self._db_path = db_path
        self._blobs_dir = blobs_dir

    async def export_jsonl(self, tenant_id: str) -> AsyncIterator[str]:
        """Yield JSONL lines describing every tenant-scoped row.

        The order is: workspaces, then for each workspace its children
        in dependency order, plus the indirect children
        (workflow_steps, workflow_step_leases) joined via workflow_id.
        Each line is a complete JSON object with a ``type`` field and
        the row columns. Blob bytes are NOT included — only the
        file_entries rows that point at them.
        """

        lines = await self._collect_lines(tenant_id)
        for line in lines:
            yield line

    async def _collect_lines(self, tenant_id: str) -> list[str]:
        out: list[str] = []
        async with connect(self._db_path) as conn:
            ws_cur = await conn.execute(
                "SELECT * FROM workspaces WHERE tenant_id = ?",
                (tenant_id,),
            )
            workspaces = list(await ws_cur.fetchall())
            await ws_cur.close()
            ws_ids = [row["id"] for row in workspaces]
            for row in workspaces:
                out.append(_row_to_jsonl(row, "workspace"))

            if not ws_ids:
                return out
            qmarks = ",".join("?" for _ in ws_ids)
            for table in _DIRECT_CHILD_TABLES:
                sql = f"SELECT * FROM {table} WHERE workspace_id IN ({qmarks})"
                cur = await conn.execute(sql, tuple(ws_ids))
                rows = list(await cur.fetchall())
                await cur.close()
                for row in rows:
                    out.append(_row_to_jsonl(row, _row_type_for(table)))

            # Indirect children: workflow_steps + workflow_step_leases.
            # Join via the workflow's workspace_id.
            cur = await conn.execute(
                f"SELECT id FROM workflows WHERE workspace_id IN ({qmarks})",
                tuple(ws_ids),
            )
            wf_ids = [row["id"] for row in await cur.fetchall()]
            await cur.close()
            if wf_ids:
                wf_qmarks = ",".join("?" for _ in wf_ids)
                cur = await conn.execute(
                    f"SELECT * FROM workflow_steps "
                    f"WHERE workflow_id IN ({wf_qmarks})",
                    tuple(wf_ids),
                )
                step_rows = list(await cur.fetchall())
                await cur.close()
                step_ids = [row["id"] for row in step_rows]
                for row in step_rows:
                    out.append(_row_to_jsonl(row, "workflow_step"))
                if step_ids:
                    step_qmarks = ",".join("?" for _ in step_ids)
                    cur = await conn.execute(
                        f"SELECT * FROM workflow_step_leases "
                        f"WHERE step_id IN ({step_qmarks})",
                        tuple(step_ids),
                    )
                    for row in await cur.fetchall():
                        out.append(_row_to_jsonl(row, "workflow_step_lease"))
                    await cur.close()
        return out

    async def delete_tenant_data(self, tenant_id: str) -> dict[str, int]:
        """Hard-delete every tenant-scoped row. Returns counts per table.

        Order: indirect children (workflow_step_leases →
        workflow_steps) first, then direct workspace children, then
        workspaces. Blob files are removed best-effort.
        """

        counts: dict[str, int] = {}
        async with connect(self._db_path) as conn:
            ws_cur = await conn.execute(
                "SELECT id FROM workspaces WHERE tenant_id = ?",
                (tenant_id,),
            )
            ws_rows = list(await ws_cur.fetchall())
            await ws_cur.close()
            ws_ids = [row["id"] for row in ws_rows]
            if ws_ids:
                qmarks = ",".join("?" for _ in ws_ids)

                # Resolve step_ids → workflow_ids → workspace_ids before any
                # DELETE so we don't lose the join key mid-cascade.
                cur = await conn.execute(
                    f"SELECT id FROM workflows WHERE workspace_id IN ({qmarks})",
                    tuple(ws_ids),
                )
                wf_ids = [row["id"] for row in await cur.fetchall()]
                await cur.close()
                step_ids: list[str] = []
                if wf_ids:
                    wf_qmarks = ",".join("?" for _ in wf_ids)
                    cur = await conn.execute(
                        f"SELECT id FROM workflow_steps "
                        f"WHERE workflow_id IN ({wf_qmarks})",
                        tuple(wf_ids),
                    )
                    step_ids = [row["id"] for row in await cur.fetchall()]
                    await cur.close()

                # Indirect children first.
                if step_ids:
                    step_qmarks = ",".join("?" for _ in step_ids)
                    cur = await conn.execute(
                        f"DELETE FROM workflow_step_leases "
                        f"WHERE step_id IN ({step_qmarks})",
                        tuple(step_ids),
                    )
                    counts["workflow_step_leases"] = cur.rowcount or 0
                    await cur.close()
                else:
                    counts["workflow_step_leases"] = 0
                if wf_ids:
                    wf_qmarks = ",".join("?" for _ in wf_ids)
                    cur = await conn.execute(
                        f"DELETE FROM workflow_steps "
                        f"WHERE workflow_id IN ({wf_qmarks})",
                        tuple(wf_ids),
                    )
                    counts["workflow_steps"] = cur.rowcount or 0
                    await cur.close()
                else:
                    counts["workflow_steps"] = 0

                # Direct children.
                for table in _DIRECT_CHILD_TABLES:
                    cur = await conn.execute(
                        f"DELETE FROM {table} WHERE workspace_id IN ({qmarks})",
                        tuple(ws_ids),
                    )
                    counts[table] = cur.rowcount or 0
                    await cur.close()
            cur = await conn.execute(
                "DELETE FROM workspaces WHERE tenant_id = ?",
                (tenant_id,),
            )
            counts["workspaces"] = cur.rowcount or 0
            await cur.close()
            await conn.commit()

        # Blob cleanup — best-effort. A failed unlink doesn't fail the
        # whole cascade; the operator mops up out-of-band.
        for ws_id in ws_ids:
            try:
                ws_dir = self._blobs_dir / ws_id
                if ws_dir.exists():
                    for child in ws_dir.rglob("*"):
                        if child.is_file():
                            try:
                                child.unlink()
                            except OSError:
                                pass
                    try:
                        ws_dir.rmdir()
                    except OSError:
                        pass
            except OSError:
                pass

        return counts


def _row_type_for(table: str) -> str:
    """Map a table name to the JSONL ``type`` field."""

    return {
        "kv_entries": "kv_entry",
        "file_entries": "file_entry",
        "channels": "channel",
        "channel_messages": "channel_message",
        "channel_consumers": "channel_consumer",
        "channel_schemas": "channel_schema",
        "snapshots": "snapshot",
        "branches": "branch",
        "workflows": "workflow",
        "workflow_steps": "workflow_step",
        "workflow_step_leases": "workflow_step_lease",
        "retention_policies": "retention_policy",
        "resource_locks": "resource_lock",
    }.get(table, table)


__all__ = ["WorkspaceComplianceStore"]
