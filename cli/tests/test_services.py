# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for ``plinth services`` (status / logs / lifecycle helpers)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from plinth_cli import settings as _s
from plinth_cli.main import cli


def _cfg(path: Path) -> Path:
    path.write_text(
        """
[default]
workspace_url = "http://workspace.test"
gateway_url   = "http://gateway.test"
identity_url  = "http://identity.test"
dashboard_url = "http://dashboard.test"
api_key       = "k"
"""
    )
    return path


@pytest.fixture
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect the global pid/log dirs into ``tmp_path``."""

    log_dir = tmp_path / "logs"
    pid_dir = tmp_path / "pids"
    log_dir.mkdir()
    pid_dir.mkdir()
    monkeypatch.setattr(_s, "LOG_DIR", log_dir)
    monkeypatch.setattr(_s, "PID_DIR", pid_dir)
    # Patch the already-imported references inside the services command module.
    from plinth_cli.commands import services as svc_cmd

    monkeypatch.setattr(svc_cmd._s, "LOG_DIR", log_dir)
    monkeypatch.setattr(svc_cmd._s, "PID_DIR", pid_dir)
    return tmp_path


@respx.mock
def test_services_status_table(runner, tmp_path: Path, isolated_dirs) -> None:
    """``services status`` reports PID + ok per service (none running here)."""

    cfg = _cfg(tmp_path / "config.toml")
    for url in (
        "http://workspace.test/healthz",
        "http://gateway.test/healthz",
        "http://identity.test/healthz",
        "http://dashboard.test/healthz",
        "http://localhost:7423/healthz",
        "http://localhost:7426/healthz",
        "http://localhost:7427/healthz",
        "http://localhost:7428/healthz",
    ):
        respx.get(url).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "--json", "services", "status"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert any(r["service"] == "workspace" for r in payload)
    assert all(r["ok"] for r in payload)


def test_services_logs_missing_file(runner, tmp_path: Path, isolated_dirs) -> None:
    """``services logs`` errors when the log file is absent."""

    cfg = _cfg(tmp_path / "config.toml")
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "services", "logs", "workspace"],
    )
    assert result.exit_code != 0


def test_services_logs_tail(runner, tmp_path: Path, isolated_dirs) -> None:
    """``services logs --tail N`` returns the last N lines."""

    cfg = _cfg(tmp_path / "config.toml")
    log_file = isolated_dirs / "logs" / "workspace.log"
    log_file.write_text("\n".join(f"line-{i}" for i in range(20)) + "\n")
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "services", "logs", "workspace", "--tail", "3"],
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.startswith("line-")]
    assert lines[-3:] == ["line-17", "line-18", "line-19"]


def test_services_logs_unknown_name(runner, tmp_path: Path) -> None:
    """Asking for a service that doesn't exist fails up front."""

    cfg = _cfg(tmp_path / "config.toml")
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "services", "logs", "nope"],
    )
    assert result.exit_code != 0


def test_services_start_without_spawn_script(
    runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``_spawn.py`` is not found we surface a friendly error."""

    from plinth_cli.commands import services as svc_cmd

    monkeypatch.setenv("PLINTH_SPAWN_SCRIPT", "/does/not/exist")
    monkeypatch.setattr(svc_cmd, "SPAWN_SCRIPT_DEFAULT", Path("/does/not/exist"))
    monkeypatch.setattr(svc_cmd, "_find_spawn_script", lambda: None)
    cfg = _cfg(tmp_path / "config.toml")
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "services", "start", "workspace"],
    )
    assert result.exit_code != 0
    assert "_spawn.py" in (result.stderr or result.output)
