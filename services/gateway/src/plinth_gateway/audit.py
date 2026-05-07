# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Audit log: persist + query invocation events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ulid import ULID

from .cache import canonical_json
from .db import Database
from .models import AuditEvent, AuditStats, AuditToolStat

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .otlp_emitter import OTLPEmitter


def new_audit_id() -> str:
    """Return a fresh ``evt_<ulid>`` identifier."""
    return f"evt_{ULID()}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value))


def _row_to_event(row) -> AuditEvent:
    return AuditEvent(
        id=row["id"],
        timestamp=_parse_ts(row["timestamp"]),
        tool_id=row["tool_id"],
        workspace_id=row["workspace_id"],
        agent_id=row["agent_id"],
        arguments_hash=row["arguments_hash"],
        arguments_preview=row["arguments_preview"],
        result_hash=row["result_hash"],
        cached=bool(row["cached"]),
        duration_ms=int(row["duration_ms"]),
        cost_estimate_usd=float(row["cost_estimate_usd"]),
        error=row["error"],
    )


@dataclass
class AuditRecord:
    """Mutable struct used by the invoke pipeline."""

    tool_id: str
    arguments: dict[str, Any]
    workspace_id: str | None
    agent_id: str | None
    arguments_hash: str
    arguments_preview: str
    cached: bool
    duration_ms: int
    cost_estimate_usd: float
    tenant_id: str = "default"
    result_hash: str | None = None
    error: str | None = None


class AuditLog:
    """Append + query the ``audit_events`` table.

    The optional ``otlp`` emitter, when provided, receives every persisted
    event for forwarding to an OTLP collector. Emission is best-effort and
    must never break the audit pipeline.
    """

    def __init__(
        self,
        db: Database,
        *,
        otlp: "OTLPEmitter | None" = None,
    ) -> None:
        self._db = db
        self._otlp = otlp

    @staticmethod
    def make_preview(arguments: dict[str, Any]) -> str:
        """First 500 chars of canonical JSON of arguments."""
        return canonical_json(arguments)[:500]

    async def record(self, rec: AuditRecord) -> AuditEvent:
        """Persist ``rec`` and return the resulting :class:`AuditEvent`."""
        event_id = new_audit_id()
        now = _utcnow()
        await self._db.execute(
            """
            INSERT INTO audit_events (
                id, timestamp, tool_id, workspace_id, agent_id, tenant_id,
                arguments_hash, arguments_preview, result_hash,
                cached, duration_ms, cost_estimate_usd, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                now.isoformat(),
                rec.tool_id,
                rec.workspace_id,
                rec.agent_id,
                rec.tenant_id,
                rec.arguments_hash,
                rec.arguments_preview,
                rec.result_hash,
                1 if rec.cached else 0,
                rec.duration_ms,
                rec.cost_estimate_usd,
                rec.error,
            ),
        )
        event = AuditEvent(
            id=event_id,
            timestamp=now,
            tool_id=rec.tool_id,
            workspace_id=rec.workspace_id,
            agent_id=rec.agent_id,
            arguments_hash=rec.arguments_hash,
            arguments_preview=rec.arguments_preview,
            result_hash=rec.result_hash,
            cached=rec.cached,
            duration_ms=rec.duration_ms,
            cost_estimate_usd=rec.cost_estimate_usd,
            error=rec.error,
        )

        # OTLP emission is strictly best-effort — failures here are counted by
        # the emitter and *must never* break the audit / invoke pipeline.
        if self._otlp is not None:
            try:
                payload = event.model_dump(mode="json")
                payload["tenant_id"] = rec.tenant_id
                payload["type"] = "tool.invoked"
                self._otlp.emit(payload)
            except Exception:  # noqa: BLE001 - swallow & move on
                pass

        return event

    async def query(
        self,
        *,
        workspace_id: str | None = None,
        tool_id: str | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Return audit events filtered + ordered newest first."""
        clauses: list[str] = []
        params: list[Any] = []
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        if tool_id is not None:
            clauses.append("tool_id = ?")
            params.append(tool_id)
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM audit_events {where} "
            "ORDER BY timestamp DESC LIMIT ?"
        )
        params.append(limit)
        rows = await self._db.fetchall(sql, tuple(params))
        return [_row_to_event(r) for r in rows]

    async def stats(
        self,
        *,
        workspace_id: str | None = None,
        tenant_id: str | None = None,
    ) -> AuditStats:
        """Aggregate stats: total invocations, cached, errors, cost, by-tool."""
        params_list: list[Any] = []
        clauses: list[str] = []
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params_list.append(workspace_id)
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params_list.append(tenant_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params: tuple[Any, ...] = tuple(params_list)

        totals_row = await self._db.fetchone(
            f"""
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(cached), 0) AS cached_count,
                   COALESCE(SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END), 0) AS errors,
                   COALESCE(SUM(cost_estimate_usd), 0) AS total_cost
            FROM audit_events {where}
            """,
            params,
        )
        total = int(totals_row["total"]) if totals_row else 0
        cached_count = int(totals_row["cached_count"]) if totals_row else 0
        error_count = int(totals_row["errors"]) if totals_row else 0
        total_cost = float(totals_row["total_cost"]) if totals_row else 0.0

        per_tool_rows = await self._db.fetchall(
            f"""
            SELECT tool_id,
                   COUNT(*) AS count,
                   COALESCE(SUM(cost_estimate_usd), 0) AS cost
            FROM audit_events {where}
            GROUP BY tool_id
            ORDER BY count DESC
            """,
            params,
        )
        by_tool = [
            AuditToolStat(
                tool_id=r["tool_id"],
                count=int(r["count"]),
                cost=float(r["cost"]),
            )
            for r in per_tool_rows
        ]

        return AuditStats(
            total_invocations=total,
            cached_count=cached_count,
            error_count=error_count,
            total_cost_usd=total_cost,
            by_tool=by_tool,
        )

    async def list_tenants(self) -> list[dict[str, Any]]:
        """Distinct tenant IDs visible across audit events + tools.

        Returns ``[{"id": ..., "audit_count": N, "tool_count": M}, ...]``.
        """

        audit_rows = await self._db.fetchall(
            "SELECT tenant_id, COUNT(*) AS c FROM audit_events GROUP BY tenant_id"
        )
        tool_rows = await self._db.fetchall(
            "SELECT tenant_id, COUNT(*) AS c FROM tools GROUP BY tenant_id"
        )

        merged: dict[str, dict[str, int]] = {}
        for row in audit_rows:
            tid = row["tenant_id"] or "default"
            merged.setdefault(tid, {"audit_count": 0, "tool_count": 0})
            merged[tid]["audit_count"] = int(row["c"])
        for row in tool_rows:
            tid = row["tenant_id"] or "default"
            merged.setdefault(tid, {"audit_count": 0, "tool_count": 0})
            merged[tid]["tool_count"] = int(row["c"])

        return sorted(
            (
                {"id": tid, **counts}
                for tid, counts in merged.items()
            ),
            key=lambda d: d["id"],
        )
