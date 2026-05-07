# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared fixtures for the durable-workflow worker tests.

Mocks both the workspace and gateway services with respx so we can
exercise the worker's poll → lease → execute → release loop without
booting real services.
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


@pytest.fixture
def workspace_mock() -> respx.MockRouter:
    with respx.mock(base_url=WORKSPACE_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def gateway_mock() -> respx.MockRouter:
    with respx.mock(base_url=GATEWAY_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def client(workspace_mock: respx.MockRouter, gateway_mock: respx.MockRouter) -> Plinth:
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
# Payload helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def make_worker(
    *,
    worker_id: str = "worker_01TEST",
    hostname: str = "test-host",
    pid: int = 1234,
    status: str = "active",
) -> dict:
    return {
        "id": worker_id,
        "hostname": hostname,
        "pid": pid,
        "started_at": _now_iso(),
        "last_heartbeat_at": _now_iso(),
        "status": status,
    }


def make_workspace(
    *,
    ws_id: str = "ws_01TEST",
    name: str = "test-ws",
) -> dict:
    return {
        "id": ws_id,
        "name": name,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "metadata": {},
    }


def make_workflow(
    *,
    wf_id: str = "wf_01TEST",
    workspace_id: str = "ws_01TEST",
    name: str = "research",
    steps_manifest: list[str] | None = None,
    steps: list[dict] | None = None,
    status: str = "running",
) -> dict:
    return {
        "id": wf_id,
        "workspace_id": workspace_id,
        "name": name,
        "steps_manifest": steps_manifest or ["search", "fetch"],
        "steps": steps or [],
        "status": status,
        "metadata": {},
        "created_at": _now_iso(),
        "started_at": _now_iso(),
        "finished_at": None,
    }


def make_workflow_step(
    *,
    step_id: str = "step_01TEST",
    workflow_id: str = "wf_01TEST",
    name: str = "search",
    status: str = "pending",
    input: dict | None = None,
    output: dict | None = None,
) -> dict:
    return {
        "id": step_id,
        "workflow_id": workflow_id,
        "name": name,
        "status": status,
        "attempt": 1,
        "started_at": _now_iso() if status != "pending" else None,
        "finished_at": _now_iso() if status in {"completed", "failed"} else None,
        "input": input,
        "output": output,
        "error": None,
        "snapshot_id": None,
        "created_at": _now_iso(),
    }


def make_lease(
    *,
    step_id: str = "step_01TEST",
    worker_id: str = "worker_01TEST",
    status: str = "running",
) -> dict:
    return {
        "step_id": step_id,
        "worker_id": worker_id,
        "acquired_at": _now_iso(),
        "expires_at": _now_iso(),
        "heartbeat_at": _now_iso(),
        "status": status,
    }


def error_envelope(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": {}}}
