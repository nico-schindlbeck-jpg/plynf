# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for ``plinth tenant``."""

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
def test_tenant_list(runner, tmp_path: Path) -> None:
    """``tenant list`` renders the JSON array verbatim."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://identity.test/v1/tenants").mock(
        return_value=httpx.Response(
            200,
            json={
                "tenants": [
                    {"id": "default", "name": "Default", "metadata": {}},
                    {"id": "acme", "name": "Acme", "metadata": {"plan": "enterprise"}},
                ]
            },
        )
    )
    result = runner.invoke(cli, ["--config", str(cfg), "--json", "tenant", "list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    ids = [t["id"] for t in payload]
    assert "default" in ids and "acme" in ids


@respx.mock
def test_tenant_create(runner, tmp_path: Path) -> None:
    """Create POSTs an ``id``/``name`` body."""

    cfg = _cfg(tmp_path / "config.toml")
    route = respx.post("http://identity.test/v1/tenants").mock(
        return_value=httpx.Response(
            201,
            json={"id": "acme", "name": "Acme", "metadata": {"plan": "ent"}},
        )
    )
    result = runner.invoke(
        cli,
        [
            "--config",
            str(cfg),
            "--json",
            "tenant",
            "create",
            "acme",
            "--name",
            "Acme",
            "--metadata",
            "plan=ent",
        ],
    )
    assert result.exit_code == 0, result.output
    sent = json.loads(route.calls[0].request.content)
    assert sent["id"] == "acme"
    assert sent["name"] == "Acme"
    assert sent["metadata"] == {"plan": "ent"}


@respx.mock
def test_tenant_show(runner, tmp_path: Path) -> None:
    """Show passes the tenant id as a path param."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://identity.test/v1/tenants/acme").mock(
        return_value=httpx.Response(
            200,
            json={"id": "acme", "name": "Acme", "metadata": {"region": "us-east-1"}},
        )
    )
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "--json", "tenant", "show", "acme"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert payload["id"] == "acme"


@respx.mock
def test_tenant_quotas_read(runner, tmp_path: Path) -> None:
    """Without ``--set``, ``quotas`` does a GET."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://identity.test/v1/tenants/acme/quotas").mock(
        return_value=httpx.Response(
            200,
            json={"max_workspaces": 100, "max_workers": 10},
        )
    )
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "--json", "tenant", "quotas", "acme"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert payload["max_workspaces"] == 100


@respx.mock
def test_tenant_quotas_set_coerces_numbers(runner, tmp_path: Path) -> None:
    """``--set max_workspaces=200`` sends an int, not a string."""

    cfg = _cfg(tmp_path / "config.toml")
    route = respx.post("http://identity.test/v1/tenants/acme/quotas").mock(
        return_value=httpx.Response(
            200,
            json={"max_workspaces": 200, "max_workers": 10},
        )
    )
    result = runner.invoke(
        cli,
        [
            "--config",
            str(cfg),
            "--json",
            "tenant",
            "quotas",
            "acme",
            "--set",
            "max_workspaces=200",
            "--set",
            "max_workers=10",
        ],
    )
    assert result.exit_code == 0, result.output
    sent = json.loads(route.calls[0].request.content)
    assert sent["max_workspaces"] == 200
    assert sent["max_workers"] == 10


@respx.mock
def test_tenant_usage(runner, tmp_path: Path) -> None:
    """Usage is a simple GET pass-through."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://identity.test/v1/tenants/acme/usage").mock(
        return_value=httpx.Response(
            200,
            json={"workspace_count": 3, "active_workers": 2},
        )
    )
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "--json", "tenant", "usage", "acme"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert payload["workspace_count"] == 3


@respx.mock
def test_tenant_export_writes_file(runner, tmp_path: Path) -> None:
    """``--output`` writes the JSON receipt to disk."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.post("http://identity.test/v1/tenants/acme/export").mock(
        return_value=httpx.Response(202, json={"export_id": "exp_1", "status": "pending"})
    )
    out_path = tmp_path / "receipt.json"
    result = runner.invoke(
        cli,
        [
            "--config",
            str(cfg),
            "--json",
            "tenant",
            "export",
            "acme",
            "--output",
            str(out_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out_path.exists()
    written = json.loads(out_path.read_text())
    assert written["export_id"] == "exp_1"


@respx.mock
def test_tenant_delete_requires_confirm(runner, tmp_path: Path) -> None:
    """Without ``--confirm`` the command fails with usage info."""

    cfg = _cfg(tmp_path / "config.toml")
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "tenant", "delete", "acme"],
    )
    assert result.exit_code != 0
    assert "confirm" in (result.stderr or result.output).lower()


@respx.mock
def test_tenant_delete_with_confirm(runner, tmp_path: Path) -> None:
    """A confirm token is forwarded as a query param."""

    cfg = _cfg(tmp_path / "config.toml")
    route = respx.delete("http://identity.test/v1/tenants/acme/data").mock(
        return_value=httpx.Response(202, json={"job_id": "job_1"})
    )
    result = runner.invoke(
        cli,
        [
            "--config",
            str(cfg),
            "--json",
            "tenant",
            "delete",
            "acme",
            "--confirm",
            "TOKEN",
        ],
    )
    assert result.exit_code == 0, result.output
    qp = dict(route.calls[0].request.url.params)
    assert qp.get("confirm") == "TOKEN"
