# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""``plinth workflow`` — list, inspect, cancel, and watch workflows.

Workflows live on the workspace service and are addressed as
``/v1/workspaces/<ws>/workflows/<wf>``. This group queries every workspace
when asked for the unscoped ``list``; pass ``--workspace`` to limit the
scan.
"""

from __future__ import annotations

import time
from typing import Any

import click

from .._http import authed_client, get_json, post_json
from ..main import CLIContext
from ..output import emit_human, emit_json, emit_table


@click.group(help="List, inspect, cancel, and watch workflows.")
def group() -> None:
    """Container for the ``workflow`` subcommands."""


@group.command("list", help="List workflows across one or all workspaces.")
@click.option("--workspace", "workspace_id", default=None, help="Restrict to one workspace.")
@click.option("--status", "status_filter", default=None, help="Filter by workflow status.")
@click.option("--limit", default=200, show_default=True, help="Max workflows to display.")
@click.pass_context
def list_workflows(
    ctx: click.Context,
    workspace_id: str | None,
    status_filter: str | None,
    limit: int,
) -> None:
    """Render a workflow table (or JSON) for the requested scope."""

    cli_ctx: CLIContext = ctx.obj
    rows = _collect_workflows(cli_ctx, workspace_id=workspace_id, status=status_filter, limit=limit)

    if cli_ctx.output_mode() == "json":
        emit_json(rows)
        return

    table_rows: list[list[str]] = []
    for r in rows:
        table_rows.append([
            r["id"],
            r["workspace_id"],
            r["name"],
            _color_status(r.get("status", "")),
            r.get("started_at") or "—",
        ])
    emit_table(
        "Workflows" + (f" — workspace {workspace_id}" if workspace_id else ""),
        ["ID", "Workspace", "Name", "Status", "Started"],
        table_rows,
    )


def _collect_workflows(
    cli_ctx: CLIContext,
    *,
    workspace_id: str | None,
    status: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Walk every workspace (or just one) and gather workflows."""

    cfg = cli_ctx.config
    with authed_client(cfg.workspace_url, cfg.api_key, timeout=cfg.timeout) as client:
        ws_ids = (
            [workspace_id] if workspace_id else _list_workspace_ids(client)
        )
        out: list[dict[str, Any]] = []
        for ws_id in ws_ids:
            code, payload = get_json(client, f"/v1/workspaces/{ws_id}/workflows")
            if code != 200 or not isinstance(payload, dict):
                continue
            for wf in payload.get("workflows", []) or []:
                if status and wf.get("status") != status:
                    continue
                out.append(_normalise_workflow(wf, ws_id))
                if len(out) >= limit:
                    return out
    return out


def _list_workspace_ids(client: Any) -> list[str]:
    """Return every workspace id visible to the bearer token."""

    code, payload = get_json(client, "/v1/workspaces")
    if code != 200 or not isinstance(payload, dict):
        return []
    return [w["id"] for w in payload.get("workspaces", []) if "id" in w]


def _normalise_workflow(wf: dict[str, Any], ws_id: str) -> dict[str, Any]:
    """Pull the columns we surface in tables/JSON."""

    return {
        "id": wf.get("id", ""),
        "workspace_id": wf.get("workspace_id", ws_id),
        "name": wf.get("name", ""),
        "status": wf.get("status", ""),
        "started_at": wf.get("started_at"),
        "finished_at": wf.get("finished_at"),
        "steps_manifest": wf.get("steps_manifest", []),
        "step_count": len(wf.get("steps") or []),
    }


def _color_status(status: str) -> str:
    """Wrap status strings in rich colours."""

    palette = {
        "completed": "[green]completed[/]",
        "running": "[cyan]running[/]",
        "failed": "[red]failed[/]",
        "cancelled": "[yellow]cancelled[/]",
        "pending": "[white]pending[/]",
    }
    return palette.get(status, status)


@group.command("show", help="Print workflow + step details.")
@click.argument("workflow_id")
@click.option(
    "--workspace",
    "workspace_id",
    default=None,
    help="Workspace id (auto-discovered if omitted).",
)
@click.pass_context
def show(ctx: click.Context, workflow_id: str, workspace_id: str | None) -> None:
    """Render a workflow + its step log."""

    cli_ctx: CLIContext = ctx.obj
    wf, ws_id = _find_workflow(cli_ctx, workflow_id, workspace_id)

    if cli_ctx.output_mode() == "json":
        emit_json(wf)
        return

    emit_human(
        f"[bold]Workflow[/] {wf['id']}\n"
        f"  workspace: {ws_id}\n"
        f"  name:      {wf.get('name')}\n"
        f"  status:    {_color_status(wf.get('status', ''))}\n"
        f"  started:   {wf.get('started_at') or '—'}\n"
        f"  finished:  {wf.get('finished_at') or '—'}\n"
    )
    rows: list[list[str]] = []
    for step in wf.get("steps") or []:
        rows.append([
            step.get("id", ""),
            step.get("name", ""),
            _color_status(step.get("status", "")),
            str(step.get("attempt", 1)),
            step.get("started_at") or "—",
            step.get("error") or "",
        ])
    emit_table(
        f"Steps ({len(rows)})",
        ["ID", "Name", "Status", "Attempt", "Started", "Error"],
        rows,
    )
    emit_human(
        f"\n[dim]Tip:[/] use `plinth workflow watch {workflow_id}` to follow live."
    )


def _find_workflow(
    cli_ctx: CLIContext,
    workflow_id: str,
    workspace_id: str | None,
) -> tuple[dict[str, Any], str]:
    """Locate a workflow by id; auto-scan workspaces if needed."""

    cfg = cli_ctx.config
    with authed_client(cfg.workspace_url, cfg.api_key, timeout=cfg.timeout) as client:
        if workspace_id:
            code, payload = get_json(
                client,
                f"/v1/workspaces/{workspace_id}/workflows/{workflow_id}",
            )
            if code == 200 and isinstance(payload, dict):
                return payload, workspace_id
            raise click.ClickException(
                f"workflow {workflow_id} not found in {workspace_id} (HTTP {code})"
            )
        for ws_id in _list_workspace_ids(client):
            code, payload = get_json(
                client,
                f"/v1/workspaces/{ws_id}/workflows/{workflow_id}",
            )
            if code == 200 and isinstance(payload, dict):
                return payload, ws_id
    raise click.ClickException(
        f"workflow {workflow_id} not found in any visible workspace"
    )


@group.command("cancel", help="Cancel a workflow.")
@click.argument("workflow_id")
@click.option("--workspace", "workspace_id", default=None, help="Workspace id (auto-discovered).")
@click.pass_context
def cancel(ctx: click.Context, workflow_id: str, workspace_id: str | None) -> None:
    """POST the cancel endpoint and surface the result."""

    cli_ctx: CLIContext = ctx.obj
    wf, ws_id = _find_workflow(cli_ctx, workflow_id, workspace_id)
    cfg = cli_ctx.config
    with authed_client(cfg.workspace_url, cfg.api_key, timeout=cfg.timeout) as client:
        code, payload = post_json(
            client,
            f"/v1/workspaces/{ws_id}/workflows/{workflow_id}/cancel",
        )
    if cli_ctx.output_mode() == "json":
        emit_json({"ok": 200 <= code < 300, "status_code": code, "body": payload})
        return
    if 200 <= code < 300:
        emit_human(f"[green]cancelled[/] {workflow_id}")
    else:
        raise click.ClickException(f"cancel failed (HTTP {code}): {payload}")


@group.command("resume", help="Print resume info — the next step + snapshot to use.")
@click.argument("workflow_id")
@click.option("--workspace", "workspace_id", default=None, help="Workspace id (auto-discovered).")
@click.pass_context
def resume(ctx: click.Context, workflow_id: str, workspace_id: str | None) -> None:
    """GET ``/v1/workspaces/<ws>/workflows/<wf>/resume`` and render the result."""

    cli_ctx: CLIContext = ctx.obj
    _, ws_id = _find_workflow(cli_ctx, workflow_id, workspace_id)
    cfg = cli_ctx.config
    with authed_client(cfg.workspace_url, cfg.api_key, timeout=cfg.timeout) as client:
        code, payload = get_json(
            client,
            f"/v1/workspaces/{ws_id}/workflows/{workflow_id}/resume",
        )
    if code != 200 or not isinstance(payload, dict):
        raise click.ClickException(f"resume info unavailable (HTTP {code}): {payload}")

    if cli_ctx.output_mode() == "json":
        emit_json(payload)
        return

    emit_human(
        f"[bold]resume info[/] for {workflow_id}\n"
        f"  workflow status: {_color_status(payload.get('workflow_status', ''))}\n"
        f"  next step:       {payload.get('next_step') or '—'}\n"
        f"  snapshot:        {payload.get('snapshot_id') or '—'}"
    )


@group.command("watch", help="Re-render the workflow every 2 seconds until it's done.")
@click.argument("workflow_id")
@click.option("--workspace", "workspace_id", default=None, help="Workspace id (auto-discovered).")
@click.option("--interval", default=2.0, show_default=True, help="Poll interval (s).")
@click.pass_context
def watch(
    ctx: click.Context,
    workflow_id: str,
    workspace_id: str | None,
    interval: float,
) -> None:
    """Loop `show` until the workflow reaches a terminal state."""

    cli_ctx: CLIContext = ctx.obj
    terminal = {"completed", "failed", "cancelled"}
    cfg = cli_ctx.config
    ws_id = workspace_id

    try:
        while True:
            click.clear()
            wf, ws_id = _find_workflow(cli_ctx, workflow_id, ws_id)
            if cli_ctx.output_mode() == "json":
                emit_json(wf)
            else:
                emit_human(
                    f"[bold]Workflow[/] {wf['id']}  "
                    f"status={_color_status(wf.get('status', ''))}"
                )
                rows = [
                    [s.get("name", ""), _color_status(s.get("status", "")), s.get("error") or ""]
                    for s in wf.get("steps") or []
                ]
                emit_table("Steps", ["Name", "Status", "Error"], rows)
            if wf.get("status") in terminal:
                return
            time.sleep(max(0.5, interval))
    except KeyboardInterrupt:
        return


__all__ = ["group"]
