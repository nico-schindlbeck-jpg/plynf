# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Audit log: persist + query invocation events.

v1.0 adds a tamper-evident hash chain — every newly recorded event carries
``prev_hash`` (the previous event's ``event_hash`` across all tenants) and
``event_hash`` = sha256(prev_hash || canonical_json(event_minus_hash)). The
:meth:`AuditLog.verify_chain` method walks the chain forward and reports
breakage at the first mismatch.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ulid import ULID

from .cache import canonical_json
from .db import Database
from .models import (
    AgentCost,
    AuditEvent,
    AuditStats,
    AuditToolStat,
    ChainVerifyResult,
    ToolUsage,
)

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


def _canonical_event_payload(row: Any) -> dict[str, Any]:
    """Return the canonical dict whose JSON gets fed into the chain hash.

    All fields except the chain columns themselves go in. Order doesn't
    matter — :func:`canonical_json` sorts keys.
    """

    return {
        "id": row["id"],
        "timestamp": str(row["timestamp"]),
        "tool_id": row["tool_id"],
        "workspace_id": row["workspace_id"],
        "agent_id": row["agent_id"],
        "tenant_id": row["tenant_id"] or "default",
        "arguments_hash": row["arguments_hash"],
        "arguments_preview": row["arguments_preview"],
        "result_hash": row["result_hash"],
        "cached": int(row["cached"] or 0),
        "duration_ms": int(row["duration_ms"]),
        "cost_estimate_usd": float(row["cost_estimate_usd"] or 0.0),
        "error": row["error"],
    }


def compute_event_hash(prev_hash: str | None, payload: dict[str, Any]) -> str:
    """Return ``sha256((prev_hash or "") || canonical_json(payload))``."""

    body = (prev_hash or "") + canonical_json(payload)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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

    async def _latest_event_hash(self) -> str | None:
        """Return the most-recent non-NULL ``event_hash``, or None."""

        row = await self._db.fetchone(
            "SELECT event_hash FROM audit_events "
            "WHERE event_hash IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        )
        if row is None:
            return None
        return row["event_hash"]

    async def record(self, rec: AuditRecord) -> AuditEvent:
        """Persist ``rec`` and return the resulting :class:`AuditEvent`.

        Computes ``prev_hash`` by reading the most-recent prior event's
        ``event_hash`` and ``event_hash`` by hashing the canonical event
        payload + prev_hash. The sequence "read latest → insert" is not
        strictly atomic across concurrent writers; the chain still detects
        tampering of any committed row, and concurrent writers simply
        produce a chain order tied to insert order.
        """

        event_id = new_audit_id()
        now = _utcnow()

        prev_hash = await self._latest_event_hash()
        # Build the canonical payload from the same column values we're
        # about to insert. Verification on the way out re-reads from the
        # DB and recomputes — keeping the two code paths in sync is the
        # point of going through the same helper.
        canonical_row = {
            "id": event_id,
            "timestamp": now.isoformat(),
            "tool_id": rec.tool_id,
            "workspace_id": rec.workspace_id,
            "agent_id": rec.agent_id,
            "tenant_id": rec.tenant_id or "default",
            "arguments_hash": rec.arguments_hash,
            "arguments_preview": rec.arguments_preview,
            "result_hash": rec.result_hash,
            "cached": 1 if rec.cached else 0,
            "duration_ms": int(rec.duration_ms),
            "cost_estimate_usd": float(rec.cost_estimate_usd),
            "error": rec.error,
        }
        event_hash = compute_event_hash(prev_hash, canonical_row)

        await self._db.execute(
            """
            INSERT INTO audit_events (
                id, timestamp, tool_id, workspace_id, agent_id, tenant_id,
                arguments_hash, arguments_preview, result_hash,
                cached, duration_ms, cost_estimate_usd, error,
                prev_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                prev_hash,
                event_hash,
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

    async def cost_by_agent(
        self,
        *,
        since: datetime,
        tenant_id: str | None = None,
        top: int = 10,
        per_agent_top_tools: int = 5,
    ) -> tuple[list[AgentCost], int, float]:
        """Aggregate cost + invocations per agent over a window.

        Args:
            since: Lower-bound timestamp; rows with ``timestamp >= since``
                are aggregated.
            tenant_id: Optional tenant filter. ``None`` = all tenants.
            top: Maximum number of agent rows to return (sorted by total
                cost desc).
            per_agent_top_tools: Maximum tools to include in each agent's
                ``top_tools`` list.

        Returns:
            ``(agents, total_agents, total_cost_usd)`` where:
              * ``agents`` is the sorted, top-N list of :class:`AgentCost`.
              * ``total_agents`` is the *unfiltered* distinct count of
                agent rows in the window (so dashboards can show
                "showing top N of M").
              * ``total_cost_usd`` is the cost sum across ALL agents in
                the window, not just the top-N.

        NULL ``agent_id`` rows are bucketed under the sentinel
        ``"(unknown)"``.
        """

        if top <= 0:
            return [], 0, 0.0

        clauses: list[str] = ["timestamp >= ?"]
        params: list[Any] = [since.isoformat()]
        if tenant_id is not None:
            clauses.append("(tenant_id = ? OR (tenant_id IS NULL AND ? = 'default'))")
            params.append(tenant_id)
            params.append(tenant_id)
        where = "WHERE " + " AND ".join(clauses)

        # Aggregate by (agent_id, tenant_id). COALESCE folds NULL agent_id
        # into the "(unknown)" bucket so dashboards always have a label.
        agent_rows = await self._db.fetchall(
            f"""
            SELECT COALESCE(agent_id, '(unknown)') AS agent_id,
                   COALESCE(tenant_id, 'default') AS tenant_id,
                   COUNT(*) AS invocations,
                   COALESCE(SUM(cached), 0) AS cached_invocations,
                   COALESCE(SUM(cost_estimate_usd), 0) AS total_cost_usd,
                   COALESCE(AVG(duration_ms), 0) AS avg_duration_ms
            FROM audit_events
            {where}
            GROUP BY COALESCE(agent_id, '(unknown)'),
                     COALESCE(tenant_id, 'default')
            ORDER BY total_cost_usd DESC, invocations DESC
            """,
            tuple(params),
        )

        total_agents = len(agent_rows)
        total_cost_usd = float(
            sum(float(row["total_cost_usd"] or 0.0) for row in agent_rows)
        )

        agent_rows = list(agent_rows)[: int(top)]
        if not agent_rows:
            return [], 0, 0.0

        # Build the top-N tool breakdown for each agent in one go. Doing
        # it per-agent in a loop keeps each statement small + parameterised
        # and avoids a window-function dialect dependency that SQLite older
        # than 3.25 doesn't support.
        agents: list[AgentCost] = []
        for row in agent_rows:
            ag_id = row["agent_id"]
            ten_id = row["tenant_id"]

            tool_clauses = list(clauses)
            tool_params = list(params)
            # ``ag_id`` may be the synthetic "(unknown)" bucket — match
            # NULL agent_id rows in that case, otherwise filter exact.
            if ag_id == "(unknown)":
                tool_clauses.append("agent_id IS NULL")
            else:
                tool_clauses.append("agent_id = ?")
                tool_params.append(ag_id)
            tool_where = "WHERE " + " AND ".join(tool_clauses)

            tool_rows = await self._db.fetchall(
                f"""
                SELECT tool_id,
                       COUNT(*) AS invocations,
                       COALESCE(SUM(cost_estimate_usd), 0) AS cost_usd
                FROM audit_events
                {tool_where}
                GROUP BY tool_id
                ORDER BY cost_usd DESC, invocations DESC
                LIMIT ?
                """,
                tuple(tool_params + [int(per_agent_top_tools)]),
            )
            top_tools = [
                ToolUsage(
                    tool_id=t["tool_id"],
                    invocations=int(t["invocations"]),
                    cost_usd=float(t["cost_usd"] or 0.0),
                )
                for t in tool_rows
            ]
            agents.append(
                AgentCost(
                    agent_id=ag_id,
                    tenant_id=ten_id,
                    invocations=int(row["invocations"]),
                    cached_invocations=int(row["cached_invocations"]),
                    total_cost_usd=float(row["total_cost_usd"] or 0.0),
                    avg_duration_ms=float(row["avg_duration_ms"] or 0.0),
                    top_tools=top_tools,
                )
            )
        return agents, total_agents, total_cost_usd

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

    # ---------------------------------------------------------------- v1.0 chain

    async def verify_chain(
        self,
        *,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> ChainVerifyResult:
        """Verify the audit hash chain forward from ``since`` (or genesis).

        Algorithm
        ---------

        1. Fetch up to ``limit`` events ordered by ``id ASC`` (ULID id sort
           = chronological insert order). When ``since`` is supplied, the
           earliest event is the first row with ``timestamp >= since``.
        2. For each event with a non-NULL ``event_hash``:
             * Recompute ``expected = sha256(prev_hash || canonical_json(event))``
               using the canonical-payload helper.
             * Compare to the stored ``event_hash``; mismatch → fail with
               ``hash_mismatch``.
             * Compare ``prev_hash`` against the previous chained event's
               ``event_hash``. If the previous event also had a hash and
               they don't match, fail with ``prev_hash_mismatch``.
        3. NULL ``event_hash`` rows (legacy / pre-v1.0) are skipped, not
           failed — the chain just doesn't extend through them.

        Returns
        -------

        :class:`ChainVerifyResult` carrying ``verified``, ``checked``
        (count of rows actually hashed), ``broken_at`` and
        ``broken_reason`` on first failure.
        """

        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM audit_events {where} ORDER BY id ASC LIMIT ?"
        params.append(int(limit))
        rows = await self._db.fetchall(sql, tuple(params))

        prev_chained_hash: str | None = None
        prev_chained_was_present = False
        checked = 0

        for row in rows:
            stored_hash = row["event_hash"]
            stored_prev = row["prev_hash"]
            if stored_hash is None:
                # Legacy row — chain doesn't extend through it. Reset the
                # "previous chained" pointer so the next hashed row is
                # treated as a chain start (its prev_hash may legitimately
                # point to whatever was the latest hashed row globally).
                continue

            payload = _canonical_event_payload(row)
            expected = compute_event_hash(stored_prev, payload)
            if expected != stored_hash:
                return ChainVerifyResult(
                    verified=False,
                    checked=checked,
                    broken_at=row["id"],
                    broken_reason="hash_mismatch",
                )

            # If this row claims a prev_hash it must match the previous
            # chained row's hash — but only when we actually saw a chained
            # row earlier in this verify pass. Otherwise prev_hash refers
            # to a row before ``since`` (or before this verify window) and
            # we trust it without re-checking.
            if prev_chained_was_present:
                if stored_prev != prev_chained_hash:
                    return ChainVerifyResult(
                        verified=False,
                        checked=checked,
                        broken_at=row["id"],
                        broken_reason="prev_hash_mismatch",
                    )

            prev_chained_hash = stored_hash
            prev_chained_was_present = True
            checked += 1

        return ChainVerifyResult(
            verified=True,
            checked=checked,
            broken_at=None,
            broken_reason=None,
        )
