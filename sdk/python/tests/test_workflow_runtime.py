# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v0.5 workflow runtime + handler decorator."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from plinth import Plinth
from plinth.exceptions import NoHandlerError
from plinth.models import WorkflowStep
from plinth.workflow_runtime import HandlerContext, WorkflowRuntime


# ---------------------------------------------------------------------------
# WorkflowRuntime
# ---------------------------------------------------------------------------


def test_register_and_dispatch_sync() -> None:
    runtime = WorkflowRuntime()

    @runtime.register("wf", "step")
    def handler(ctx):
        return {"ok": True}

    ctx = MagicMock(spec=HandlerContext)
    result = asyncio.run(runtime.dispatch("wf", "step", ctx))
    assert result == {"ok": True}


def test_register_and_dispatch_async() -> None:
    runtime = WorkflowRuntime()

    @runtime.register("wf", "step")
    async def handler(ctx):
        await asyncio.sleep(0)
        return 42

    ctx = MagicMock(spec=HandlerContext)
    result = asyncio.run(runtime.dispatch("wf", "step", ctx))
    assert result == 42


def test_dispatch_no_handler_raises() -> None:
    runtime = WorkflowRuntime()
    ctx = MagicMock(spec=HandlerContext)
    with pytest.raises(NoHandlerError):
        asyncio.run(runtime.dispatch("missing", "step", ctx))


def test_register_duplicate_raises() -> None:
    runtime = WorkflowRuntime()

    @runtime.register("wf", "step")
    def first(ctx):
        return 1

    with pytest.raises(ValueError) as info:

        @runtime.register("wf", "step")  # noqa: F811
        def second(ctx):
            return 2

    assert "already registered" in str(info.value)


def test_register_validates_keys() -> None:
    runtime = WorkflowRuntime()
    with pytest.raises(ValueError):
        runtime.register("", "step")
    with pytest.raises(ValueError):
        runtime.register("wf", "")


def test_runtime_introspection() -> None:
    runtime = WorkflowRuntime()
    assert len(runtime) == 0
    assert ("wf", "step") not in runtime

    @runtime.register("wf", "step")
    def handler(ctx):
        return None

    assert len(runtime) == 1
    assert ("wf", "step") in runtime
    assert runtime.has("wf", "step")
    assert not runtime.has("wf", "missing")
    assert runtime.get("wf", "step") is handler
    assert runtime.get("wf", "missing") is None
    assert runtime.keys() == [("wf", "step")]


def test_handler_context_exposes_step_and_tools() -> None:
    fake_client = MagicMock()
    fake_client.tools = "tools-sentinel"
    fake_workspace = MagicMock()
    fake_workflow = MagicMock()
    step = WorkflowStep(
        id="step_1",
        workflow_id="wf_1",
        name="search",
        status="running",
        attempt=1,
        input={"topic": "x"},
        output=None,
        error=None,
        snapshot_id=None,
    )
    ctx = HandlerContext(
        client=fake_client,
        workspace=fake_workspace,
        workflow=fake_workflow,
        step=step,
        worker_id="worker_1",
    )
    assert ctx.step is step
    assert ctx.client is fake_client
    assert ctx.workspace is fake_workspace
    assert ctx.workflow is fake_workflow
    assert ctx.worker_id == "worker_1"
    assert ctx.tools == "tools-sentinel"


# ---------------------------------------------------------------------------
# Plinth.workflow_handler integration
# ---------------------------------------------------------------------------


def test_client_exposes_workflow_runtime(client: Plinth) -> None:
    runtime = client.workflow_runtime
    assert isinstance(runtime, WorkflowRuntime)
    # Same identity each time (module-level cache).
    assert client.workflow_runtime is runtime


def test_client_workflow_handler_registers(client: Plinth) -> None:
    @client.workflow_handler("research", step="search")
    def search_handler(ctx):
        return ctx.step.input

    assert client.workflow_runtime.has("research", "search")


def test_client_workflow_handler_duplicate_raises(client: Plinth) -> None:
    @client.workflow_handler("research", step="search")
    def first(ctx):
        return None

    with pytest.raises(ValueError):

        @client.workflow_handler("research", step="search")
        def second(ctx):  # noqa: F811
            return None
