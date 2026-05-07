# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Direct tests for ``registry.Registry``."""

from __future__ import annotations

import pytest

from plinth_gateway.exceptions import ToolAlreadyExists, ToolNotFound
from plinth_gateway.models import ToolRegistration
from plinth_gateway.registry import Registry


def _payload(tool_id: str = "fs.read") -> ToolRegistration:
    return ToolRegistration(
        tool_id=tool_id,
        name=tool_id,
        description=f"{tool_id} tool",
        transport="http",
        endpoint=f"http://mcp.test/invoke/{tool_id.split('.')[-1]}",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        idempotent=True,
        side_effects="read",
        cache_ttl_seconds=120,
        auth_method="none",
        auth_config={},
    )


async def test_register_and_get(db) -> None:
    reg = Registry(db)
    tool = await reg.register(_payload())
    assert tool.tool_id == "fs.read"
    assert tool.idempotent is True
    fetched = await reg.get("fs.read")
    assert fetched.endpoint == tool.endpoint


async def test_register_duplicate_raises(db) -> None:
    reg = Registry(db)
    await reg.register(_payload())
    with pytest.raises(ToolAlreadyExists):
        await reg.register(_payload())


async def test_get_missing_raises(db) -> None:
    reg = Registry(db)
    with pytest.raises(ToolNotFound):
        await reg.get("missing")


async def test_get_optional(db) -> None:
    reg = Registry(db)
    assert await reg.get_optional("missing") is None
    await reg.register(_payload())
    got = await reg.get_optional("fs.read")
    assert got is not None and got.tool_id == "fs.read"


async def test_list_orders_by_creation(db) -> None:
    reg = Registry(db)
    await reg.register(_payload("fs.read"))
    await reg.register(_payload("fs.write"))
    tools = await reg.list()
    assert [t.tool_id for t in tools] == ["fs.read", "fs.write"]


async def test_delete_removes_tool_and_cache(db) -> None:
    reg = Registry(db)
    await reg.register(_payload("fs.read"))
    # Insert a fake cache entry to verify cascade
    await db.execute(
        """
        INSERT INTO cache_entries
          (cache_key, tool_id, arguments_hash, result, created_at, expires_at, hit_count)
        VALUES ('k1', 'fs.read', 'h', '{}', '2026-01-01T00:00:00+00:00',
                '2099-01-01T00:00:00+00:00', 0)
        """
    )
    await reg.delete("fs.read")
    with pytest.raises(ToolNotFound):
        await reg.get("fs.read")
    rows = await db.fetchall("SELECT * FROM cache_entries WHERE tool_id = 'fs.read'")
    assert rows == []


async def test_delete_missing_raises(db) -> None:
    reg = Registry(db)
    with pytest.raises(ToolNotFound):
        await reg.delete("missing")


def test_registry_parse_ts_branches() -> None:
    from plinth_gateway import registry as reg_mod
    from datetime import datetime, timezone

    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert reg_mod._parse_ts(aware) is aware
    naive = datetime(2026, 1, 1)
    assert reg_mod._parse_ts(naive).tzinfo == timezone.utc
