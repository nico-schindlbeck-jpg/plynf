# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Append-only replication log for cross-region replay (v1.0 scaffolding).

Plinth's v1.0 multi-region story is **opt-in scaffolding**: this module
gives operators a way to record every mutation that happens in a primary
deployment and pull/replay it on a replica. The actual cross-region
orchestration (cron, k8s sidecar, agent) is left to the operator — see
``docs/multi-region.md`` for the playbook.

Wire format::

    GET  /v1/admin/replication/log?since=<seq>&limit=<int>  → list[Entry]
    POST /v1/admin/replication/apply   body: list[Entry]    → {applied,skipped}
    GET  /v1/admin/replication/status                       → {mode,seq,...}

For SQLite the log lives in a single ``replication_log`` table; for
Postgres operators are expected to use streaming replication via a
managed service and leave this module unused.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

UTC = timezone.utc  # noqa: UP017


REPLICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS replication_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  workspace_id TEXT,
  payload TEXT NOT NULL,
  occurred_at TIMESTAMP NOT NULL,
  region_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_repl_seq ON replication_log(seq);
"""


@dataclass
class ReplicationEntry:
    """A single mutation captured for cross-region replay."""

    seq: int
    kind: str
    workspace_id: str | None
    payload: dict[str, Any]
    occurred_at: datetime
    region_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "workspace_id": self.workspace_id,
            "payload": self.payload,
            "occurred_at": self.occurred_at.isoformat(),
            "region_id": self.region_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ReplicationEntry:
        occurred = raw.get("occurred_at")
        if isinstance(occurred, str):
            ts = datetime.fromisoformat(occurred)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        elif isinstance(occurred, datetime):
            ts = occurred if occurred.tzinfo else occurred.replace(tzinfo=UTC)
        else:
            ts = datetime.now(UTC)
        return cls(
            seq=int(raw["seq"]),
            kind=str(raw["kind"]),
            workspace_id=raw.get("workspace_id"),
            payload=raw.get("payload") or {},
            occurred_at=ts,
            region_id=str(raw.get("region_id") or "default"),
        )


class ReplicationLog:
    """Persistent append-only log of mutations.

    ``ReplicationLog`` is created eagerly per app, but is a no-op when the
    parent service isn't in primary mode — the API surface is the same
    either way so a deployment can flip ``replication_mode`` without code
    changes. ``apply_entries`` is for replicas pulling entries from a
    primary; the seq number is preserved so replicas can dedupe on retry.
    """

    def __init__(self, db_path: Path, *, region_id: str) -> None:
        self._db_path = db_path
        self._region_id = region_id
        self._initialised = False

    async def init(self) -> None:
        """Idempotently create the ``replication_log`` table.

        Calling ``append`` lazily triggers init; tests + the lifespan
        handler call this directly so the table exists before a status
        probe runs.
        """

        async with self._connect() as conn:
            await conn.executescript(REPLICATION_SCHEMA)
            await conn.commit()
        self._initialised = True

    @contextlib.asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._db_path)
        try:
            await conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = aiosqlite.Row
            yield conn
        finally:
            await conn.close()

    async def append(
        self,
        kind: str,
        payload: dict[str, Any],
        workspace_id: str | None = None,
    ) -> int:
        """Persist one mutation and return its sequence number.

        Used by the primary's mutating routes. Standalone deployments
        won't call this — the workspace api wires it via a thin guard
        that checks ``settings.replication_mode``.
        """

        if not self._initialised:
            await self.init()
        now = datetime.now(UTC).isoformat()
        async with self._connect() as conn:
            cur = await conn.execute(
                """
                INSERT INTO replication_log
                    (kind, workspace_id, payload, occurred_at, region_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (kind, workspace_id, json.dumps(payload), now, self._region_id),
            )
            await conn.commit()
            return int(cur.lastrowid or 0)

    async def fetch(
        self,
        since: int = 0,
        limit: int = 1000,
    ) -> list[ReplicationEntry]:
        """Return entries with ``seq > since`` (capped by ``limit``)."""

        if not self._initialised:
            await self.init()
        async with self._connect() as conn:
            cur = await conn.execute(
                """
                SELECT seq, kind, workspace_id, payload, occurred_at, region_id
                FROM replication_log
                WHERE seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (since, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        out: list[ReplicationEntry] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"]) if row["payload"] else {}
            except json.JSONDecodeError:
                payload = {}
            ts_raw = row["occurred_at"]
            ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else ts_raw
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            out.append(
                ReplicationEntry(
                    seq=int(row["seq"]),
                    kind=str(row["kind"]),
                    workspace_id=row["workspace_id"],
                    payload=payload,
                    occurred_at=ts,
                    region_id=str(row["region_id"]),
                )
            )
        return out

    async def current_seq(self) -> int:
        """Return the highest seq number persisted (0 when empty)."""

        if not self._initialised:
            await self.init()
        async with self._connect() as conn:
            cur = await conn.execute("SELECT MAX(seq) AS s FROM replication_log")
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            return 0
        value = row["s"] if "s" in row.keys() else row[0]
        return int(value or 0)

    async def apply_entries(
        self,
        entries: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Ingest entries pulled from a primary peer.

        Returns ``(applied, skipped)`` counts. Entries whose ``seq`` is
        already present locally are skipped — replicas can safely retry
        an apply call without duplicating state.
        """

        if not self._initialised:
            await self.init()

        applied = 0
        skipped = 0
        async with self._connect() as conn:
            for raw in entries:
                try:
                    parsed = ReplicationEntry.from_dict(raw)
                except (KeyError, ValueError, TypeError):
                    skipped += 1
                    continue
                # Skip dupes — replicas may retry partial pulls.
                cur = await conn.execute(
                    "SELECT 1 FROM replication_log WHERE seq = ?",
                    (parsed.seq,),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is not None:
                    skipped += 1
                    continue
                await conn.execute(
                    """
                    INSERT INTO replication_log
                        (seq, kind, workspace_id, payload, occurred_at, region_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parsed.seq,
                        parsed.kind,
                        parsed.workspace_id,
                        json.dumps(parsed.payload),
                        parsed.occurred_at.isoformat(),
                        parsed.region_id,
                    ),
                )
                applied += 1
            await conn.commit()
        return applied, skipped


__all__ = [
    "REPLICATION_SCHEMA",
    "ReplicationEntry",
    "ReplicationLog",
]
