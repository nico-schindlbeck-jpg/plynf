# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for ``plinth migrate``."""

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
def test_migrate_status_one_service(runner, tmp_path: Path) -> None:
    """Status reports applied + pending migration ids."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://workspace.test/v1/admin/migrations").mock(
        return_value=httpx.Response(
            200,
            json={
                "applied": [
                    {"id": "0001_initial"},
                    {"id": "0002_channels"},
                ],
                "pending": [
                    {"id": "0007_new"},
                ],
            },
        )
    )
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "--json", "migrate", "status", "workspace"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert payload["service"] == "workspace"
    assert payload["applied_count"] == 2
    assert payload["pending"] == ["0007_new"]


@respx.mock
def test_migrate_status_all_services(runner, tmp_path: Path) -> None:
    """``status all`` queries every service in turn."""

    cfg = _cfg(tmp_path / "config.toml")
    for url in (
        "http://workspace.test/v1/admin/migrations",
        "http://gateway.test/v1/admin/migrations",
        "http://identity.test/v1/admin/migrations",
    ):
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                json={"applied": [{"id": "0001"}], "pending": []},
            )
        )
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "--json", "migrate", "status", "all"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    services = sorted(p["service"] for p in payload)
    assert services == ["gateway", "identity", "workspace"]


@respx.mock
def test_migrate_apply(runner, tmp_path: Path) -> None:
    """``apply`` POSTs to the apply endpoint."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.post("http://workspace.test/v1/admin/migrations/apply").mock(
        return_value=httpx.Response(200, json={"applied": [{"id": "0007_new"}]})
    )
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "--json", "migrate", "apply", "workspace"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert payload["ok"] is True


@respx.mock
def test_migrate_apply_failure(runner, tmp_path: Path) -> None:
    """A 500 response surfaces a friendly CLI error."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.post("http://workspace.test/v1/admin/migrations/apply").mock(
        return_value=httpx.Response(500, json={"error": "schema lock"})
    )
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "migrate", "apply", "workspace"],
    )
    assert result.exit_code != 0
    assert "apply failed" in (result.stderr or result.output)


@respx.mock
def test_migrate_rollback_to(runner, tmp_path: Path) -> None:
    """``rollback-to`` sends ``{"to": ..., "dry_run": ...}``."""

    cfg = _cfg(tmp_path / "config.toml")
    route = respx.post("http://workspace.test/v1/admin/migrations/rollback").mock(
        return_value=httpx.Response(200, json={"rolled_back": ["0007_new"]})
    )
    result = runner.invoke(
        cli,
        [
            "--config",
            str(cfg),
            "--json",
            "migrate",
            "rollback-to",
            "workspace",
            "0006_resource_locks",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    sent = json.loads(route.calls[0].request.content)
    assert sent["to"] == "0006_resource_locks"
    assert sent["dry_run"] is True


@respx.mock
def test_migrate_unknown_service(runner, tmp_path: Path) -> None:
    """Unknown service names raise a friendly error before any HTTP call."""

    cfg = _cfg(tmp_path / "config.toml")
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "migrate", "status", "bogus"],
    )
    assert result.exit_code != 0


def test_migrate_create_surfaces_command(runner, tmp_path: Path) -> None:
    """``migrate create`` doesn't hit the network — it prints the right hint."""

    cfg = _cfg(tmp_path / "config.toml")
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "migrate", "create", "workspace", "add_thing"],
    )
    assert result.exit_code == 0
    assert "python -m plinth_workspace migrate --create" in result.output
