# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tool registration and lookup against SQLite."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .db import Database
from .exceptions import ToolAlreadyExists, ToolNotFound
from .models import Tool, ToolRegistration


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_tool(row) -> Tool:
    return Tool(
        tool_id=row["tool_id"],
        name=row["name"],
        description=row["description"],
        transport=row["transport"],
        endpoint=row["endpoint"],
        input_schema=json.loads(row["input_schema"]),
        output_schema=json.loads(row["output_schema"]),
        idempotent=bool(row["idempotent"]),
        side_effects=row["side_effects"],
        cache_ttl_seconds=row["cache_ttl_seconds"],
        auth_method=row["auth_method"],
        auth_config=json.loads(row["auth_config"]),
        created_at=_parse_ts(row["created_at"]),
        updated_at=_parse_ts(row["updated_at"]),
    )


def _row_tenant(row) -> str:
    """Best-effort lookup of the row's ``tenant_id`` column (defaults to default)."""

    try:
        value = row["tenant_id"]
    except (KeyError, IndexError):
        return "default"
    return value or "default"


def _parse_ts(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value))


class Registry:
    """CRUD over the ``tools`` table."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def register(
        self,
        payload: ToolRegistration,
        *,
        tenant_id: str = "default",
    ) -> Tool:
        """Insert a new tool. Raises :class:`ToolAlreadyExists` on duplicate."""
        existing = await self._db.fetchone(
            "SELECT tool_id FROM tools WHERE tool_id = ?", (payload.tool_id,)
        )
        if existing is not None:
            raise ToolAlreadyExists(
                f"Tool {payload.tool_id!r} is already registered",
                details={"tool_id": payload.tool_id},
            )

        now = _utcnow()
        await self._db.execute(
            """
            INSERT INTO tools (
                tool_id, name, description, transport, endpoint,
                input_schema, output_schema, idempotent, side_effects,
                cache_ttl_seconds, auth_method, auth_config, tenant_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.tool_id,
                payload.name,
                payload.description,
                payload.transport,
                payload.endpoint,
                json.dumps(payload.input_schema),
                json.dumps(payload.output_schema),
                1 if payload.idempotent else 0,
                payload.side_effects,
                payload.cache_ttl_seconds,
                payload.auth_method,
                json.dumps(payload.auth_config),
                tenant_id,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        return await self.get(payload.tool_id)

    async def get(
        self,
        tool_id: str,
        *,
        tenant_id: str | None = None,
    ) -> Tool:
        """Return one tool. Raises :class:`ToolNotFound` if missing or
        in a different tenant.
        """

        row = await self._db.fetchone(
            "SELECT * FROM tools WHERE tool_id = ?", (tool_id,)
        )
        if row is None:
            raise ToolNotFound(
                f"Tool {tool_id!r} is not registered",
                details={"tool_id": tool_id},
            )
        if tenant_id is not None and _row_tenant(row) != tenant_id:
            raise ToolNotFound(
                f"Tool {tool_id!r} is not registered",
                details={"tool_id": tool_id},
            )
        return _row_to_tool(row)

    async def get_optional(self, tool_id: str) -> Tool | None:
        """Return one tool or ``None`` if not registered."""
        row = await self._db.fetchone(
            "SELECT * FROM tools WHERE tool_id = ?", (tool_id,)
        )
        return _row_to_tool(row) if row else None

    async def list(
        self,
        *,
        tenant_id: str | None = None,
    ) -> list[Tool]:
        """Return all tools, ordered by creation time. Optionally filter by tenant."""

        if tenant_id is None:
            rows = await self._db.fetchall(
                "SELECT * FROM tools ORDER BY created_at ASC"
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM tools WHERE tenant_id = ? ORDER BY created_at ASC",
                (tenant_id,),
            )
        return [_row_to_tool(r) for r in rows]

    async def delete(self, tool_id: str) -> None:
        """Remove a tool. Raises :class:`ToolNotFound` if missing."""
        existing = await self._db.fetchone(
            "SELECT tool_id FROM tools WHERE tool_id = ?", (tool_id,)
        )
        if existing is None:
            raise ToolNotFound(
                f"Tool {tool_id!r} is not registered",
                details={"tool_id": tool_id},
            )
        await self._db.execute("DELETE FROM tools WHERE tool_id = ?", (tool_id,))
        # Best-effort: clear cache entries for this tool
        await self._db.execute("DELETE FROM cache_entries WHERE tool_id = ?", (tool_id,))
