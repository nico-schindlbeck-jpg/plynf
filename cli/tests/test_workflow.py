# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for ``plinth workflow`` (list / show / cancel / resume)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from plinth_cli.main import cli


def _cfg(path: Path) -> Path:
    path.write_text(
        """
[default]
workspace_url = "http://workspace.test"
gateway_url   = "http://gateway.test"
identity_url  = "http://identity.test"
api_key       = "k"
"""
    )
    return path


@respx.mock
def test_workflow_list_all_workspaces(runner, tmp_path: Path) -> None:
    """``workflow list`` walks every workspace + collects workflows."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://workspace.test/v1/workspaces").mock(
        return_value=httpx.Response(
            200,
            json={
                "workspaces": [
                    {"id": "ws_a", "name": "research-task"},
                    {"id": "ws_b", "name": "data-import"},
                ]
            },
        )
    )
    respx.get("http://workspace.test/v1/workspaces/ws_a/workflows").mock(
        return_value=httpx.Response(
            200,
            json={
                "workflows": [
                    {
                        "id": "wf_1",
                        "workspace_id": "ws_a",
                        "name": "research",
                        "status": "running",
                        "started_at": "2026-05-08T10:00:00Z",
                    }
                ]
            },
        )
    )
    respx.get("http://workspace.test/v1/workspaces/ws_b/workflows").mock(
        return_value=httpx.Response(
            200,
            json={
                "workflows": [
                    {
                        "id": "wf_2",
                        "workspace_id": "ws_b",
                        "name": "etl",
                        "status": "completed",
                        "started_at": "2026-05-08T09:00:00Z",
                    }
                ]
            },
        )
    )
    result = runner.invoke(cli, ["--config", str(cfg), "--json", "workflow", "list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    ids = sorted(w["id"] for w in payload)
    assert ids == ["wf_1", "wf_2"]


@respx.mock
def test_workflow_list_status_filter(runner, tmp_path: Path) -> None:
    """``--status`` filters in-process so the JSON only includes matches."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://workspace.test/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [{"id": "ws_a"}]}),
    )
    respx.get("http://workspace.test/v1/workspaces/ws_a/workflows").mock(
        return_value=httpx.Response(
            200,
            json={
                "workflows": [
                    {"id": "wf_1", "name": "x", "status": "running"},
                    {"id": "wf_2", "name": "y", "status": "completed"},
                ]
            },
        )
    )
    result = runner.invoke(
        cli,
        [
            "--config",
            str(cfg),
            "--json",
            "workflow",
            "list",
            "--status",
            "completed",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert [w["id"] for w in payload] == ["wf_2"]


@respx.mock
def test_workflow_show_auto_locates(runner, tmp_path: Path) -> None:
    """``workflow show`` walks workspaces until the id is found."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://workspace.test/v1/workspaces").mock(
        return_value=httpx.Response(
            200,
            json={"workspaces": [{"id": "ws_a"}, {"id": "ws_b"}]},
        ),
    )
    respx.get("http://workspace.test/v1/workspaces/ws_a/workflows/wf_x").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )
    respx.get("http://workspace.test/v1/workspaces/ws_b/workflows/wf_x").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "wf_x",
                "workspace_id": "ws_b",
                "name": "found",
                "status": "running",
                "steps": [{"id": "s1", "name": "first", "status": "completed"}],
            },
        )
    )
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "--json", "workflow", "show", "wf_x"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert payload["id"] == "wf_x"
    assert payload["workspace_id"] == "ws_b"


@respx.mock
def test_workflow_show_explicit_workspace_404(runner, tmp_path: Path) -> None:
    """When ``--workspace`` is given, a 404 is surfaced instantly."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://workspace.test/v1/workspaces/ws_a/workflows/wf_y").mock(
        return_value=httpx.Response(404, json={"detail": "missing"})
    )
    result = runner.invoke(
        cli,
        [
            "--config",
            str(cfg),
            "workflow",
            "show",
            "wf_y",
            "--workspace",
            "ws_a",
        ],
    )
    assert result.exit_code != 0


@respx.mock
def test_workflow_cancel(runner, tmp_path: Path) -> None:
    """A successful cancel returns ``ok=True``."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://workspace.test/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [{"id": "ws_a"}]})
    )
    respx.get("http://workspace.test/v1/workspaces/ws_a/workflows/wf_1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "wf_1",
                "workspace_id": "ws_a",
                "name": "x",
                "status": "running",
                "steps": [],
            },
        )
    )
    respx.post("http://workspace.test/v1/workspaces/ws_a/workflows/wf_1/cancel").mock(
        return_value=httpx.Response(200, json={"id": "wf_1", "status": "cancelled"})
    )
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "--json", "workflow", "cancel", "wf_1"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert payload["ok"] is True


@respx.mock
def test_workflow_resume(runner, tmp_path: Path) -> None:
    """Resume info is surfaced verbatim from the server."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://workspace.test/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [{"id": "ws_a"}]})
    )
    respx.get("http://workspace.test/v1/workspaces/ws_a/workflows/wf_1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "wf_1",
                "workspace_id": "ws_a",
                "name": "x",
                "status": "running",
                "steps": [],
            },
        )
    )
    respx.get("http://workspace.test/v1/workspaces/ws_a/workflows/wf_1/resume").mock(
        return_value=httpx.Response(
            200,
            json={
                "workflow_id": "wf_1",
                "workflow_status": "running",
                "next_step": "second",
                "snapshot_id": "snap_1",
            },
        )
    )
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "--json", "workflow", "resume", "wf_1"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert payload["next_step"] == "second"
    assert payload["snapshot_id"] == "snap_1"
