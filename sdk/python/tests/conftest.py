# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared pytest fixtures for the Plinth SDK test suite.

Every test runs offline: we wire the SDK's HTTP clients to ``respx``
mock transports, so no real network traffic is generated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx

from plinth import Plinth

WORKSPACE_URL = "http://workspace.test"
GATEWAY_URL = "http://gateway.test"


# ---------------------------------------------------------------------------
# Respx mock routers — one per service, both bound to the same client.
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace_mock() -> respx.MockRouter:
    """A respx router for the workspace service."""
    with respx.mock(base_url=WORKSPACE_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def gateway_mock() -> respx.MockRouter:
    """A respx router for the gateway service."""
    with respx.mock(base_url=GATEWAY_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def client(
    workspace_mock: respx.MockRouter,
    gateway_mock: respx.MockRouter,
) -> Plinth:
    """A ``Plinth`` instance wired to the two respx routers."""
    plinth_client = Plinth(
        workspace_url=WORKSPACE_URL,
        gateway_url=GATEWAY_URL,
        api_key="test-key",
        workspace_transport=httpx.MockTransport(workspace_mock.handler),
        gateway_transport=httpx.MockTransport(gateway_mock.handler),
    )
    yield plinth_client
    plinth_client.close()


# ---------------------------------------------------------------------------
# Payload builders — keep tests readable.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()  # noqa: UP017


def make_workspace(
    *,
    ws_id: str = "ws_01TESTWORKSPACE",
    name: str = "research-task-1",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": ws_id,
        "name": name,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "metadata": metadata or {},
    }


def make_kv_entry(
    *,
    workspace_id: str = "ws_01TESTWORKSPACE",
    key: str,
    value: Any,
    version: int = 1,
    branch_id: str | None = None,
    deleted: bool = False,
) -> dict[str, Any]:
    return {
        "workspace_id": workspace_id,
        "key": key,
        "value": value,
        "version": version,
        "created_at": _now_iso(),
        "deleted": deleted,
        "branch_id": branch_id,
    }


def make_file_entry(
    *,
    workspace_id: str = "ws_01TESTWORKSPACE",
    path: str,
    size: int = 0,
    sha256: str = "0" * 64,
    content_type: str = "text/plain; charset=utf-8",
    version: int = 1,
    branch_id: str | None = None,
) -> dict[str, Any]:
    return {
        "workspace_id": workspace_id,
        "path": path,
        "size": size,
        "sha256": sha256,
        "content_type": content_type,
        "version": version,
        "created_at": _now_iso(),
        "deleted": False,
        "branch_id": branch_id,
    }


def make_snapshot(
    *,
    snap_id: str = "snap_01TESTSNAPSHOT",
    workspace_id: str = "ws_01TESTWORKSPACE",
    name: str = "baseline",
    message: str | None = None,
) -> dict[str, Any]:
    return {
        "id": snap_id,
        "workspace_id": workspace_id,
        "name": name,
        "message": message,
        "created_at": _now_iso(),
        "kv_versions": {},
        "file_versions": {},
        "parent_snapshot_id": None,
    }


def make_branch(
    *,
    branch_id: str = "br_01TESTBRANCH",
    workspace_id: str = "ws_01TESTWORKSPACE",
    name: str = "experiment",
    from_snapshot_id: str = "snap_01TESTSNAPSHOT",
) -> dict[str, Any]:
    return {
        "id": branch_id,
        "workspace_id": workspace_id,
        "name": name,
        "from_snapshot_id": from_snapshot_id,
        "created_at": _now_iso(),
        "merged": False,
        "merged_at": None,
    }


def make_tool(
    *,
    tool_id: str = "web.fetch",
    name: str = "web.fetch",
    description: str = "Fetch a URL.",
) -> dict[str, Any]:
    return {
        "tool_id": tool_id,
        "name": name,
        "description": description,
        "transport": "http",
        "endpoint": "http://mock-mcp/invoke/web.fetch",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "idempotent": True,
        "side_effects": "read",
        "cache_ttl_seconds": 300,
        "auth_method": "none",
        "auth_config": {},
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }


def make_invoke_response(
    *,
    tool_id: str = "web.fetch",
    arguments: dict[str, Any] | None = None,
    result: Any = None,
    cached: bool = False,
    duration_ms: int = 42,
) -> dict[str, Any]:
    return {
        "tool_id": tool_id,
        "arguments": arguments or {},
        "result": result if result is not None else {"content": "hello", "status": 200},
        "cached": cached,
        "duration_ms": duration_ms,
        "audit_id": "evt_01TESTAUDIT",
        "cost_estimate_usd": 0.0,
    }


def make_audit_event(
    *,
    event_id: str = "evt_01TESTAUDIT",
    tool_id: str = "web.fetch",
    workspace_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "timestamp": _now_iso(),
        "tool_id": tool_id,
        "workspace_id": workspace_id,
        "agent_id": None,
        "arguments_hash": "deadbeef",
        "result_hash": "cafef00d",
        "cached": False,
        "duration_ms": 17,
        "cost_estimate_usd": 0.0,
        "error": None,
    }


def make_channel_message(
    *,
    msg_id: str = "msg_01TESTMESSAGE",
    channel: str = "research-out",
    workspace_id: str = "ws_01TESTWORKSPACE",
    seq: int = 1,
    payload: Any = None,
    sender: str | None = None,
    type: str | None = None,
    correlation_id: str | None = None,
    headers: dict[str, str] | None = None,
    delivered_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "channel": channel,
        "workspace_id": workspace_id,
        "seq": seq,
        "payload": payload if payload is not None else {"hello": "world"},
        "sender": sender,
        "type": type,
        "correlation_id": correlation_id,
        "headers": headers or {},
        "sent_at": _now_iso(),
        "delivered_at": delivered_at,
    }


def make_channel(
    *,
    name: str = "research-out",
    workspace_id: str = "ws_01TESTWORKSPACE",
    message_count: int = 0,
    last_send_at: str | None = None,
    last_receive_at: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "workspace_id": workspace_id,
        "message_count": message_count,
        "created_at": _now_iso(),
        "last_send_at": last_send_at,
        "last_receive_at": last_receive_at,
    }


def make_workflow_step(
    *,
    step_id: str = "step_01TESTSTEP",
    workflow_id: str = "wf_01TESTWORKFLOW",
    name: str = "search",
    status: str = "running",
    attempt: int = 1,
    input: Any = None,
    output: Any = None,
    error: str | None = None,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "workflow_id": workflow_id,
        "name": name,
        "status": status,
        "attempt": attempt,
        "started_at": _now_iso() if status != "pending" else None,
        "finished_at": _now_iso() if status in {"completed", "failed", "cancelled"} else None,
        "input": input,
        "output": output,
        "error": error,
        "snapshot_id": snapshot_id,
        "created_at": _now_iso(),
    }


def make_workflow(
    *,
    wf_id: str = "wf_01TESTWORKFLOW",
    workspace_id: str = "ws_01TESTWORKSPACE",
    name: str = "research-pipeline",
    steps_manifest: list[str] | None = None,
    steps: list[dict[str, Any]] | None = None,
    status: str = "pending",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": wf_id,
        "workspace_id": workspace_id,
        "name": name,
        "steps_manifest": steps_manifest or ["search", "fetch", "extract", "synthesize"],
        "steps": steps or [],
        "status": status,
        "metadata": metadata or {},
        "created_at": _now_iso(),
        "started_at": _now_iso() if status != "pending" else None,
        "finished_at": _now_iso() if status in {"completed", "failed", "cancelled"} else None,
    }


def make_resume_info(
    *,
    workflow_id: str = "wf_01TESTWORKFLOW",
    workflow_status: str = "running",
    next_step: str | None = "fetch",
    last_completed: dict[str, Any] | None = None,
    snapshot_id: str | None = "snap_01TESTSNAPSHOT",
) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "workflow_status": workflow_status,
        "next_step": next_step,
        "last_completed": last_completed,
        "snapshot_id": snapshot_id,
    }


def error_envelope(code: str, message: str) -> dict[str, Any]:
    """Build the standard ``{"error": {...}}`` envelope used by services."""
    return {"error": {"code": code, "message": message, "details": {}}}
