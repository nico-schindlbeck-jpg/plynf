# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""``plinth audit`` — query the gateway audit log."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import click

from .._http import authed_client, get_json
from ..main import CLIContext
from ..output import emit_csv, emit_human, emit_json, emit_table

_DURATION_RE = re.compile(r"^(?P<num>\d+)(?P<unit>[smhd])$")


def parse_since(value: str | None) -> str | None:
    """Convert ``"1h"`` / ``"30m"`` / an ISO timestamp to ISO-UTC.

    Returns ``None`` for falsy input (server applies its default window).
    """

    if not value:
        return None
    m = _DURATION_RE.match(value.strip())
    if m:
        num = int(m.group("num"))
        unit = m.group("unit")
        delta = {
            "s": timedelta(seconds=num),
            "m": timedelta(minutes=num),
            "h": timedelta(hours=num),
            "d": timedelta(days=num),
        }[unit]
        return (datetime.now(timezone.utc) - delta).isoformat()
    # Try parsing as ISO-8601; fall through unchanged on failure (server validates).
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    except ValueError as exc:
        raise click.ClickException(
            f"could not parse --since={value!r}: expected '<n>{{s,m,h,d}}' or ISO-8601"
        ) from exc


@click.group(invoke_without_command=True, help="Query the gateway audit log.")
@click.option("--tool", "tool_id", default=None, help="Filter by tool id.")
@click.option("--workspace", "workspace_id", default=None, help="Filter by workspace id.")
@click.option(
    "--tenant",
    "tenant_id",
    default=None,
    help="Filter by tenant id (post-fetch since the gateway audit endpoint is workspace-scoped).",
)
@click.option("--since", default=None, help="Relative window (e.g. 1h, 30m) or ISO timestamp.")
@click.option("--limit", default=50, show_default=True, help="Maximum events to display.")
@click.pass_context
def group(
    ctx: click.Context,
    tool_id: str | None,
    workspace_id: str | None,
    tenant_id: str | None,
    since: str | None,
    limit: int,
) -> None:
    """Default action: query the audit log with the requested filters.

    Subcommands (``stats`` / ``tail``) take their own options; the root
    invocation hits ``GET /v1/audit`` directly.
    """

    if ctx.invoked_subcommand is not None:
        return

    cli_ctx: CLIContext = ctx.obj
    events = _query(
        cli_ctx,
        tool_id=tool_id,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        since=since,
        limit=limit,
    )
    _emit_events(cli_ctx, events)


def _query(
    cli_ctx: CLIContext,
    *,
    tool_id: str | None,
    workspace_id: str | None,
    tenant_id: str | None,
    since: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Issue a ``GET /v1/audit`` and return its events (filtered by tenant)."""

    cfg = cli_ctx.config
    params = {
        "tool_id": tool_id,
        "workspace_id": workspace_id,
        "since": parse_since(since),
        "limit": int(limit),
    }
    with authed_client(cfg.gateway_url, cfg.api_key, timeout=cfg.timeout) as client:
        code, body = get_json(client, "/v1/audit", params=params)
    if code != 200 or not isinstance(body, dict):
        raise click.ClickException(
            f"audit query failed (HTTP {code}): {body}"
        )
    events = body.get("events", []) or []
    if tenant_id:
        events = [e for e in events if e.get("tenant_id") == tenant_id]
    return events


def _emit_events(cli_ctx: CLIContext, events: list[dict[str, Any]]) -> None:
    """Render the events list."""

    mode = cli_ctx.output_mode()
    if mode == "json":
        emit_json(events)
        return

    columns = [
        "Timestamp",
        "Tool",
        "Workspace",
        "Cached",
        "Duration",
        "Cost",
        "Error",
    ]
    rows: list[list[str]] = []
    for e in events:
        rows.append([
            e.get("timestamp") or "",
            e.get("tool_id") or "",
            e.get("workspace_id") or "—",
            "✔" if e.get("cached") else "",
            f"{e.get('duration_ms', '')}ms",
            f"${e.get('cost_estimate_usd', 0):.4f}",
            (e.get("error") or "")[:40],
        ])
    if mode == "csv":
        emit_csv(columns, rows)
        return
    emit_table(f"Audit ({len(rows)} events)", columns, rows)


@group.command("stats", help="Show aggregated audit stats (last 24h).")
@click.option("--workspace", "workspace_id", default=None, help="Restrict to one workspace.")
@click.pass_context
def stats(ctx: click.Context, workspace_id: str | None) -> None:
    """GET ``/v1/audit/stats`` and render."""

    cli_ctx: CLIContext = ctx.obj
    cfg = cli_ctx.config
    with authed_client(cfg.gateway_url, cfg.api_key, timeout=cfg.timeout) as client:
        code, body = get_json(client, "/v1/audit/stats", params={"workspace_id": workspace_id})
    if code != 200:
        raise click.ClickException(f"stats query failed (HTTP {code}): {body}")
    if isinstance(body, dict) and "stats" in body and isinstance(body["stats"], dict):
        body = body["stats"]
    if cli_ctx.output_mode() == "json":
        emit_json(body)
        return
    pairs = body if isinstance(body, dict) else {"raw": body}
    emit_human("[bold]Audit stats[/]")
    rows = [[k, str(v)] for k, v in pairs.items()]
    emit_table("Counter", ["Metric", "Value"], rows)


@group.command("tail", help="Follow new audit events (poll every 2s).")
@click.option("--tool", "tool_id", default=None, help="Filter by tool id.")
@click.option("--workspace", "workspace_id", default=None, help="Filter by workspace id.")
@click.option("--tenant", "tenant_id", default=None, help="Filter by tenant id.")
@click.option("--interval", default=2.0, show_default=True, help="Poll interval (s).")
@click.option("--limit", default=20, show_default=True, help="Page size per poll.")
@click.pass_context
def tail(
    ctx: click.Context,
    tool_id: str | None,
    workspace_id: str | None,
    tenant_id: str | None,
    interval: float,
    limit: int,
) -> None:
    """Poll loop that prints new events as they arrive."""

    cli_ctx: CLIContext = ctx.obj
    seen: set[str] = set()
    since = parse_since("5m") or ""
    try:
        while True:
            events = _query(
                cli_ctx,
                tool_id=tool_id,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                since=since,
                limit=limit,
            )
            new = [e for e in events if e.get("id") not in seen]
            for e in new:
                seen.add(e.get("id", ""))
            if new:
                _emit_events(cli_ctx, new)
            time.sleep(max(0.5, float(interval)))
    except KeyboardInterrupt:
        return


__all__ = ["group", "parse_since"]
