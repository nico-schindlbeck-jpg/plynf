# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the durable workflow worker.

Each test wires up the workspace + gateway via respx, then drives the
worker through one or more poll → lease → execute → release iterations.
We exercise ``Worker._poll_lease_and_execute`` directly so the slot
loop's idle-backoff doesn't slow tests down.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from plinth import Plinth
from plinth.workflow_runtime import WorkflowRuntime

from plinth_workflow_worker.worker import Worker

from tests.conftest import (
    error_envelope,
    make_lease,
    make_worker,
    make_workflow,
    make_workflow_step,
    make_workspace,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_worker(
    client: Plinth,
    *,
    runtime: WorkflowRuntime | None = None,
    concurrency: int = 1,
    workspace_filter: list[str] | None = None,
) -> Worker:
    """Build a worker with deterministic timing for tests."""
    return Worker(
        client,
        runtime=runtime or client.workflow_runtime,
        concurrency=concurrency,
        lease_ttl=30,
        heartbeat_interval=5,
        worker_heartbeat_interval=10,
        poll_interval=0.05,
        workspace_filter=workspace_filter,
    )


def _wire_register(workspace_mock: respx.MockRouter) -> None:
    workspace_mock.post("/v1/workers/register").mock(
        return_value=httpx.Response(201, json=make_worker())
    )
    workspace_mock.post("/v1/workers/worker_01TEST/heartbeat").mock(
        return_value=httpx.Response(200, json=make_worker())
    )
    workspace_mock.post("/v1/workers/worker_01TEST/drain").mock(
        return_value=httpx.Response(200, json=make_worker(status="draining"))
    )


# ---------------------------------------------------------------------------
# Worker registration + heartbeat
# ---------------------------------------------------------------------------


async def test_worker_init_validates_heartbeat_below_ttl(client: Plinth) -> None:
    with pytest.raises(ValueError):
        Worker(client, lease_ttl=10, heartbeat_interval=10)
    with pytest.raises(ValueError):
        Worker(client, lease_ttl=10, heartbeat_interval=20)


async def test_worker_registers_then_drains_on_shutdown(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    _wire_register(workspace_mock)
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": []})
    )

    @client.workflow_handler("any", step="any")
    def handler(ctx):
        return None

    worker = _build_worker(client)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.1)  # let it register
    assert worker.worker_id == "worker_01TEST"
    worker.stop()
    await asyncio.wait_for(task, timeout=2.0)
    # Drain endpoint should have been hit.
    assert any(
        "drain" in str(call.request.url) for call in workspace_mock.calls
    )


# ---------------------------------------------------------------------------
# Poll → lease → execute → release happy path
# ---------------------------------------------------------------------------


async def test_poll_lease_execute_release_happy_path(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    _wire_register(workspace_mock)

    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [make_workspace()]})
    )
    workspace_mock.post("/v1/workspaces").mock(
        return_value=httpx.Response(201, json=make_workspace())
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows").mock(
        return_value=httpx.Response(
            200,
            json={"workflows": [make_workflow(name="research")]},
        )
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows/wf_01TEST").mock(
        return_value=httpx.Response(200, json=make_workflow(name="research"))
    )
    workspace_mock.get(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/pending"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "steps": [
                    make_workflow_step(
                        name="search",
                        input={"topic": "renewable energy"},
                    )
                ]
            },
        )
    )
    workspace_mock.post(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/steps/step_01TEST/lease"
    ).mock(return_value=httpx.Response(200, json=make_lease()))
    workspace_mock.post(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/steps/step_01TEST/release"
    ).mock(return_value=httpx.Response(200, json=make_lease(status="released")))

    handler_calls = []

    @client.workflow_handler("research", step="search")
    def handler(ctx):
        handler_calls.append({"input": ctx.step.input, "worker": ctx.worker_id})
        return {"sources": ["a", "b"]}

    worker = _build_worker(client)
    # Need to register first for the worker_id to be set.
    worker.worker_id = client.workers.register().id

    log = worker._log
    claimed = await worker._poll_lease_and_execute(log)
    assert claimed is True
    assert len(handler_calls) == 1
    assert handler_calls[0]["input"] == {"topic": "renewable energy"}
    assert worker.stats["leased"] == 1
    assert worker.stats["completed"] == 1


async def test_handler_raises_marks_failed(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    _wire_register(workspace_mock)

    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [make_workspace()]})
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows").mock(
        return_value=httpx.Response(200, json={"workflows": [make_workflow(name="research")]})
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows/wf_01TEST").mock(
        return_value=httpx.Response(200, json=make_workflow(name="research"))
    )
    workspace_mock.get(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/pending"
    ).mock(
        return_value=httpx.Response(
            200, json={"steps": [make_workflow_step(name="search")]}
        )
    )
    workspace_mock.post(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/steps/step_01TEST/lease"
    ).mock(return_value=httpx.Response(200, json=make_lease()))

    captured_release = {}

    def release_handler(request: httpx.Request) -> httpx.Response:
        import json

        captured_release["body"] = json.loads(request.read())
        return httpx.Response(200, json=make_lease(status="released"))

    workspace_mock.post(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/steps/step_01TEST/release"
    ).mock(side_effect=release_handler)

    @client.workflow_handler("research", step="search")
    def handler(ctx):
        raise RuntimeError("synthetic boom")

    worker = _build_worker(client)
    worker.worker_id = client.workers.register().id
    log = worker._log
    claimed = await worker._poll_lease_and_execute(log)
    assert claimed is True
    assert captured_release["body"]["status"] == "failed"
    assert "synthetic boom" in captured_release["body"]["error"]
    assert worker.stats["failed"] == 1


# ---------------------------------------------------------------------------
# Lease conflict (someone else got it)
# ---------------------------------------------------------------------------


async def test_lease_conflict_returns_no_claim(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    _wire_register(workspace_mock)
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [make_workspace()]})
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows").mock(
        return_value=httpx.Response(200, json={"workflows": [make_workflow(name="research")]})
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows/wf_01TEST").mock(
        return_value=httpx.Response(200, json=make_workflow(name="research"))
    )
    workspace_mock.get(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/pending"
    ).mock(
        return_value=httpx.Response(
            200, json={"steps": [make_workflow_step(name="search")]}
        )
    )
    workspace_mock.post(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/steps/step_01TEST/lease"
    ).mock(
        return_value=httpx.Response(
            409, json=error_envelope("LEASE_CONFLICT", "another worker beat us")
        )
    )

    @client.workflow_handler("research", step="search")
    def handler(ctx):  # pragma: no cover - never invoked
        return None

    worker = _build_worker(client)
    worker.worker_id = client.workers.register().id
    log = worker._log
    claimed = await worker._poll_lease_and_execute(log)
    assert claimed is False
    assert worker.stats["lost"] == 1


# ---------------------------------------------------------------------------
# No pending steps
# ---------------------------------------------------------------------------


async def test_no_pending_steps_does_nothing(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    _wire_register(workspace_mock)
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [make_workspace()]})
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows").mock(
        return_value=httpx.Response(200, json={"workflows": [make_workflow(name="research")]})
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows/wf_01TEST").mock(
        return_value=httpx.Response(200, json=make_workflow(name="research"))
    )
    workspace_mock.get(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/pending"
    ).mock(return_value=httpx.Response(200, json={"steps": []}))

    @client.workflow_handler("research", step="search")
    def handler(ctx):  # pragma: no cover
        return None

    worker = _build_worker(client)
    worker.worker_id = client.workers.register().id
    log = worker._log
    claimed = await worker._poll_lease_and_execute(log)
    assert claimed is False
    assert worker.stats == {"leased": 0, "completed": 0, "failed": 0, "lost": 0}


# ---------------------------------------------------------------------------
# Skipping workflows we don't have handlers for
# ---------------------------------------------------------------------------


async def test_skips_workflow_without_matching_handler(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    _wire_register(workspace_mock)
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [make_workspace()]})
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows").mock(
        return_value=httpx.Response(
            200, json={"workflows": [make_workflow(name="other")]}
        )
    )

    @client.workflow_handler("research", step="search")
    def handler(ctx):  # pragma: no cover
        return None

    worker = _build_worker(client)
    worker.worker_id = client.workers.register().id
    log = worker._log
    claimed = await worker._poll_lease_and_execute(log)
    assert claimed is False


async def test_only_processes_workflows_with_handler(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    """Two workflows in the same workspace; only one matches our handler."""

    _wire_register(workspace_mock)
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [make_workspace()]})
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows").mock(
        return_value=httpx.Response(
            200,
            json={
                "workflows": [
                    make_workflow(wf_id="wf_match", name="research"),
                    make_workflow(wf_id="wf_skip", name="other"),
                ]
            },
        )
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows/wf_match").mock(
        return_value=httpx.Response(
            200, json=make_workflow(wf_id="wf_match", name="research")
        )
    )
    workspace_mock.get(
        "/v1/workspaces/ws_01TEST/workflows/wf_match/pending"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "steps": [
                    make_workflow_step(
                        step_id="step_match", workflow_id="wf_match", name="search"
                    )
                ]
            },
        )
    )
    workspace_mock.post(
        "/v1/workspaces/ws_01TEST/workflows/wf_match/steps/step_match/lease"
    ).mock(return_value=httpx.Response(200, json=make_lease(step_id="step_match")))
    workspace_mock.post(
        "/v1/workspaces/ws_01TEST/workflows/wf_match/steps/step_match/release"
    ).mock(return_value=httpx.Response(200, json=make_lease(step_id="step_match", status="released")))

    @client.workflow_handler("research", step="search")
    def handler(ctx):
        return {"ok": True}

    worker = _build_worker(client)
    worker.worker_id = client.workers.register().id
    log = worker._log
    claimed = await worker._poll_lease_and_execute(log)
    assert claimed is True
    assert worker.stats["completed"] == 1


# ---------------------------------------------------------------------------
# Workspace filter
# ---------------------------------------------------------------------------


async def test_workspace_filter_excludes_unmatched(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    _wire_register(workspace_mock)
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(
            200,
            json={
                "workspaces": [
                    make_workspace(ws_id="ws_a", name="alpha"),
                    make_workspace(ws_id="ws_b", name="beta"),
                ]
            },
        )
    )

    @client.workflow_handler("research", step="search")
    def handler(ctx):  # pragma: no cover
        return None

    worker = _build_worker(client, workspace_filter=["ws_a"])
    worker.worker_id = client.workers.register().id
    # Only ws_a should be polled. ws_b's listing route should be unused.
    workspace_mock.get("/v1/workspaces/ws_a/workflows").mock(
        return_value=httpx.Response(200, json={"workflows": []})
    )
    log = worker._log
    claimed = await worker._poll_lease_and_execute(log)
    assert claimed is False


# ---------------------------------------------------------------------------
# Async handler
# ---------------------------------------------------------------------------


async def test_async_handler_supported(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    _wire_register(workspace_mock)
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [make_workspace()]})
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows").mock(
        return_value=httpx.Response(200, json={"workflows": [make_workflow(name="research")]})
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows/wf_01TEST").mock(
        return_value=httpx.Response(200, json=make_workflow(name="research"))
    )
    workspace_mock.get(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/pending"
    ).mock(
        return_value=httpx.Response(
            200, json={"steps": [make_workflow_step(name="search")]}
        )
    )
    workspace_mock.post(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/steps/step_01TEST/lease"
    ).mock(return_value=httpx.Response(200, json=make_lease()))
    workspace_mock.post(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/steps/step_01TEST/release"
    ).mock(return_value=httpx.Response(200, json=make_lease(status="released")))

    @client.workflow_handler("research", step="search")
    async def handler(ctx):
        await asyncio.sleep(0)
        return {"async": True}

    worker = _build_worker(client)
    worker.worker_id = client.workers.register().id
    log = worker._log
    assert await worker._poll_lease_and_execute(log)
    assert worker.stats["completed"] == 1


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


def test_cli_requires_handlers_module(monkeypatch) -> None:
    from plinth_workflow_worker.__main__ import main

    rc = main(["--api-key", "k"])
    assert rc == 2


def test_cli_settings_round_trip(monkeypatch) -> None:
    """``_build_settings`` should layer CLI flags on top of env defaults."""

    from plinth_workflow_worker.__main__ import _build_arg_parser, _build_settings

    parser = _build_arg_parser()
    args = parser.parse_args(
        [
            "--workspace-url",
            "http://ws.example",
            "--api-key",
            "abc",
            "--concurrency",
            "8",
            "--lease-ttl",
            "120",
            "--heartbeat-interval",
            "30",
            "--handlers-module",
            "myapp.handlers",
        ]
    )
    settings = _build_settings(args)
    assert settings.workspace_url == "http://ws.example"
    assert settings.api_key == "abc"
    assert settings.concurrency == 8
    assert settings.lease_ttl == 120
    assert settings.heartbeat_interval == 30
    assert settings.handlers_module == "myapp.handlers"


# ---------------------------------------------------------------------------
# Concurrency: two workers contending for one step
# ---------------------------------------------------------------------------


async def test_two_workers_contend_for_same_step(
    workspace_mock: respx.MockRouter, gateway_mock: respx.MockRouter
) -> None:
    """Spec requirement: when two workers try to lease the same step,
    exactly one wins.

    The mocked lease endpoint flips between 200 and 409: first caller
    wins, second loses. We assert the bookkeeping reflects that.
    """

    workspace_mock.post("/v1/workers/register").mock(
        return_value=httpx.Response(201, json=make_worker())
    )
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [make_workspace()]})
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows").mock(
        return_value=httpx.Response(
            200, json={"workflows": [make_workflow(name="research")]}
        )
    )
    workspace_mock.get("/v1/workspaces/ws_01TEST/workflows/wf_01TEST").mock(
        return_value=httpx.Response(200, json=make_workflow(name="research"))
    )
    workspace_mock.get(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/pending"
    ).mock(
        return_value=httpx.Response(
            200, json={"steps": [make_workflow_step(name="search")]}
        )
    )

    call_count = {"n": 0}

    def lease_handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(200, json=make_lease())
        return httpx.Response(
            409, json=error_envelope("LEASE_CONFLICT", "lost the race")
        )

    workspace_mock.post(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/steps/step_01TEST/lease"
    ).mock(side_effect=lease_handler)
    workspace_mock.post(
        "/v1/workspaces/ws_01TEST/workflows/wf_01TEST/steps/step_01TEST/release"
    ).mock(return_value=httpx.Response(200, json=make_lease(status="released")))

    # Two clients sharing the same mock transport.
    a = Plinth(
        workspace_url="http://workspace.test",
        gateway_url="http://gateway.test",
        api_key="a",
        workspace_transport=httpx.MockTransport(workspace_mock.handler),
        gateway_transport=httpx.MockTransport(gateway_mock.handler),
    )
    b = Plinth(
        workspace_url="http://workspace.test",
        gateway_url="http://gateway.test",
        api_key="b",
        workspace_transport=httpx.MockTransport(workspace_mock.handler),
        gateway_transport=httpx.MockTransport(gateway_mock.handler),
    )

    @a.workflow_handler("research", step="search")
    def a_handler(ctx):
        return {"who": "a"}

    @b.workflow_handler("research", step="search")
    def b_handler(ctx):
        return {"who": "b"}

    wa = _build_worker(a)
    wa.worker_id = a.workers.register().id
    wb = _build_worker(b)
    wb.worker_id = b.workers.register().id

    a_claim = await wa._poll_lease_and_execute(wa._log)
    b_claim = await wb._poll_lease_and_execute(wb._log)
    assert sorted([a_claim, b_claim]) == [False, True]
    assert (wa.stats["completed"] + wb.stats["completed"]) == 1
    assert (wa.stats["lost"] + wb.stats["lost"]) == 1

    a.close()
    b.close()
