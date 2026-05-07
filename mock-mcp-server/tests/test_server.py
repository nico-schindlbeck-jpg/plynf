# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the FastAPI surface of the Mock MCP Server."""

from __future__ import annotations

import httpx
import pytest

from mock_mcp import __version__


@pytest.mark.asyncio
async def test_healthz_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "service": "mock-mcp",
    }


@pytest.mark.asyncio
async def test_tools_lists_six_tools(client: httpx.AsyncClient) -> None:
    response = await client.get("/tools")
    assert response.status_code == 200
    data = response.json()
    assert "tools" in data
    tool_ids = [t["tool_id"] for t in data["tools"]]
    assert sorted(tool_ids) == sorted(
        ["web.fetch", "web.search", "fs.read", "fs.write", "notes.add", "notes.list"]
    )
    # Spot-check schema completeness on one tool.
    web_fetch = next(t for t in data["tools"] if t["tool_id"] == "web.fetch")
    assert web_fetch["idempotent"] is True
    assert web_fetch["side_effects"] == "read"
    assert web_fetch["cache_ttl_seconds"] == 3600
    assert "input_schema" in web_fetch and "output_schema" in web_fetch
    # Every tool has the expected metadata keys.
    expected_keys = {
        "tool_id", "name", "description", "input_schema", "output_schema",
        "idempotent", "side_effects", "cache_ttl_seconds",
    }
    for tool in data["tools"]:
        assert expected_keys.issubset(tool.keys())


@pytest.mark.asyncio
async def test_invoke_unknown_tool_returns_404_with_envelope(client: httpx.AsyncClient) -> None:
    response = await client.post("/invoke/does.not.exist", json={})
    assert response.status_code == 404
    payload = response.json()
    assert "error" in payload
    assert payload["error"]["code"] == "TOOL_NOT_FOUND"
    assert "does.not.exist" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_invoke_invalid_json_returns_400(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/invoke/web.fetch",
        content=b"not json{",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_invoke_non_object_body_returns_400(client: httpx.AsyncClient) -> None:
    response = await client.post("/invoke/web.fetch", json=[1, 2, 3])
    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_invoke_empty_body_treated_as_empty_args(client: httpx.AsyncClient) -> None:
    """notes.list takes no args; an empty body should still work."""
    response = await client.post(
        "/invoke/notes.list",
        content=b"",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"result": {"notes": []}}
