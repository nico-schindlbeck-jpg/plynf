# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for ``plinth audit`` (filters, stats, error handling)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import respx

from plinth_cli.commands.audit import parse_since
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


# ---------------------------------------------------------------------------
# parse_since unit tests
# ---------------------------------------------------------------------------


def test_parse_since_relative_minutes() -> None:
    out = parse_since("30m")
    assert out is not None
    parsed = datetime.fromisoformat(out)
    delta = datetime.now(timezone.utc) - parsed
    assert timedelta(minutes=29) < delta < timedelta(minutes=31)


def test_parse_since_relative_hours() -> None:
    out = parse_since("2h")
    assert out is not None
    parsed = datetime.fromisoformat(out)
    delta = datetime.now(timezone.utc) - parsed
    assert timedelta(hours=1, minutes=59) < delta < timedelta(hours=2, minutes=1)


def test_parse_since_iso_passthrough() -> None:
    iso = "2026-05-08T12:00:00+00:00"
    assert parse_since(iso) == iso


def test_parse_since_none() -> None:
    assert parse_since(None) is None
    assert parse_since("") is None


def test_parse_since_invalid() -> None:
    import click as _click
    import pytest as _pytest

    with _pytest.raises(_click.ClickException):
        parse_since("garbage")


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


@respx.mock
def test_audit_basic_query(runner, tmp_path: Path) -> None:
    """No filters → query the gateway audit endpoint with limit only."""

    cfg = _cfg(tmp_path / "config.toml")
    route = respx.get("http://gateway.test/v1/audit").mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [
                    {
                        "id": "evt_1",
                        "timestamp": "2026-05-08T10:00:00Z",
                        "tool_id": "web.search",
                        "workspace_id": "ws_a",
                        "tenant_id": "default",
                        "cached": False,
                        "duration_ms": 120,
                        "cost_estimate_usd": 0.001,
                    }
                ]
            },
        )
    )
    result = runner.invoke(cli, ["--config", str(cfg), "--json", "audit"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert payload[0]["tool_id"] == "web.search"
    # Confirms ``Authorization`` header was attached.
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer k"


@respx.mock
def test_audit_filter_by_tool(runner, tmp_path: Path) -> None:
    """The ``--tool`` flag becomes a query parameter."""

    cfg = _cfg(tmp_path / "config.toml")
    route = respx.get("http://gateway.test/v1/audit").mock(
        return_value=httpx.Response(200, json={"events": []})
    )
    result = runner.invoke(
        cli,
        [
            "--config",
            str(cfg),
            "--json",
            "audit",
            "--tool",
            "web.search",
            "--limit",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    qp = dict(route.calls[0].request.url.params)
    assert qp.get("tool_id") == "web.search"
    assert qp.get("limit") == "5"


@respx.mock
def test_audit_tenant_filter_post_fetch(runner, tmp_path: Path) -> None:
    """Tenant filtering is client-side (the endpoint doesn't accept it)."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://gateway.test/v1/audit").mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [
                    {"id": "1", "tool_id": "a", "tenant_id": "x"},
                    {"id": "2", "tool_id": "b", "tenant_id": "y"},
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
            "audit",
            "--tenant",
            "x",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert [e["id"] for e in payload] == ["1"]


@respx.mock
def test_audit_stats(runner, tmp_path: Path) -> None:
    """``audit stats`` unwraps ``{"stats": {...}}`` payloads."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://gateway.test/v1/audit/stats").mock(
        return_value=httpx.Response(
            200,
            json={"stats": {"total": 42, "cached_pct": 50}},
        )
    )
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "--json", "audit", "stats"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert payload["total"] == 42


@respx.mock
def test_audit_failure_friendly_error(runner, tmp_path: Path) -> None:
    """A 500 from the server becomes a one-line CLI error."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://gateway.test/v1/audit").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    result = runner.invoke(cli, ["--config", str(cfg), "audit"])
    assert result.exit_code != 0
    assert "audit query failed" in (result.stderr or result.output)


@respx.mock
def test_audit_csv_format(runner, tmp_path: Path) -> None:
    """``--output csv`` emits a CSV-formatted event list."""

    cfg = _cfg(tmp_path / "config.toml")
    respx.get("http://gateway.test/v1/audit").mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [
                    {
                        "id": "evt_1",
                        "timestamp": "2026-05-08T10:00:00Z",
                        "tool_id": "web.search",
                        "workspace_id": "ws_a",
                        "cached": False,
                        "duration_ms": 120,
                        "cost_estimate_usd": 0.001,
                    }
                ]
            },
        )
    )
    result = runner.invoke(
        cli,
        ["--config", str(cfg), "--output", "csv", "audit"],
    )
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "Timestamp,Tool,Workspace,Cached,Duration,Cost,Error"
    assert "web.search" in lines[1]
