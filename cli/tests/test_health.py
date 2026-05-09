# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for ``plinth health`` against mocked services."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from plinth_cli.main import cli


def _write_config(path: Path) -> Path:
    path.write_text(
        """
[default]
workspace_url = "http://workspace.test"
gateway_url   = "http://gateway.test"
identity_url  = "http://identity.test"
dashboard_url = "http://dashboard.test"
api_key       = "test"
output        = "json"
"""
    )
    return path


@respx.mock
def test_health_all_services_ok(runner, tmp_path: Path) -> None:
    """Every probed service returns 200 → exit 0 + ``ok=True`` per row."""

    cfg = _write_config(tmp_path / "config.toml")
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
        respx.get(url).mock(
            return_value=httpx.Response(200, json={"status": "ok", "version": "1.0.0"})
        )

    result = runner.invoke(cli, ["--config", str(cfg), "--json", "health"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert payload["workspace"]["ok"] is True
    assert payload["gateway"]["ok"] is True
    assert payload["identity"]["ok"] is True


@respx.mock
def test_health_one_service_down_exits_nonzero(runner, tmp_path: Path) -> None:
    """A single failure makes the whole command non-zero."""

    cfg = _write_config(tmp_path / "config.toml")
    respx.get("http://workspace.test/healthz").mock(
        return_value=httpx.Response(503, json={"status": "down"})
    )
    for url in (
        "http://gateway.test/healthz",
        "http://identity.test/healthz",
        "http://dashboard.test/healthz",
        "http://localhost:7423/healthz",
        "http://localhost:7426/healthz",
        "http://localhost:7427/healthz",
        "http://localhost:7428/healthz",
    ):
        respx.get(url).mock(return_value=httpx.Response(200, json={"status": "ok"}))

    result = runner.invoke(cli, ["--config", str(cfg), "--json", "health"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout.strip())
    assert payload["workspace"]["ok"] is False


@respx.mock
def test_health_connection_error(runner, tmp_path: Path) -> None:
    """A transport-level error surfaces as ``ok=False`` with an error string."""

    cfg = _write_config(tmp_path / "config.toml")
    respx.get("http://workspace.test/healthz").mock(
        side_effect=httpx.ConnectError("boom")
    )
    for url in (
        "http://gateway.test/healthz",
        "http://identity.test/healthz",
        "http://dashboard.test/healthz",
        "http://localhost:7423/healthz",
        "http://localhost:7426/healthz",
        "http://localhost:7427/healthz",
        "http://localhost:7428/healthz",
    ):
        respx.get(url).mock(return_value=httpx.Response(200, json={"status": "ok"}))

    result = runner.invoke(cli, ["--config", str(cfg), "--json", "health"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout.strip())
    assert payload["workspace"]["ok"] is False
    assert "boom" in payload["workspace"].get("error", "")


@respx.mock
def test_health_human_output_table(runner, tmp_path: Path) -> None:
    """Default human mode renders a status table with checkmarks."""

    cfg = _write_config(tmp_path / "config.toml")
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
        ["--config", str(cfg), "--output", "human", "health"],
    )
    assert result.exit_code == 0
    assert "Plinth health" in result.output
