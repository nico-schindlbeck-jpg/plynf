# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""``plinth migrate`` — schema migration control plane.

Each Plinth service exposes an admin migration HTTP surface at
``/v1/admin/migrations`` (status), ``/v1/admin/migrations/apply``, and
``/v1/admin/migrations/rollback``. This group provides a uniform CLI on
top of those endpoints + service-specific URL routing.
"""

from __future__ import annotations

from typing import Any

import click

from .._http import authed_client, get_json, post_json
from ..main import CLIContext
from ..output import emit_human, emit_json, emit_table

# Map ``<service>`` argument values to their config URL field.
_SERVICE_TO_FIELD: dict[str, str] = {
    "workspace": "workspace_url",
    "gateway": "gateway_url",
    "identity": "identity_url",
}


@click.group(
    invoke_without_command=True,
    help="Apply schema migrations on workspace/gateway/identity.",
)
@click.pass_context
def group(ctx: click.Context) -> None:
    """Show help when called with no subcommand."""

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _service_url(cli_ctx: CLIContext, service: str) -> str:
    """Resolve the base URL for ``service`` from config."""

    field = _SERVICE_TO_FIELD.get(service)
    if field is None:
        raise click.ClickException(
            f"unknown service {service!r}. known: workspace, gateway, identity"
        )
    return getattr(cli_ctx.config, field)


@group.command(
    "status",
    help="Show applied + pending migrations for one or all services.",
)
@click.argument("service", default="all")
@click.pass_context
def status(ctx: click.Context, service: str) -> None:
    """Render a per-service migration status report."""

    cli_ctx: CLIContext = ctx.obj

    if service == "all":
        rows: list[dict[str, Any]] = []
        for svc in _SERVICE_TO_FIELD:
            rows.append(_status_one(cli_ctx, svc))
        if cli_ctx.output_mode() == "json":
            emit_json(rows)
            return
        _emit_status_table(rows)
        return

    row = _status_one(cli_ctx, service)
    if cli_ctx.output_mode() == "json":
        emit_json(row)
        return
    _emit_status_table([row])


def _status_one(cli_ctx: CLIContext, service: str) -> dict[str, Any]:
    """GET ``/v1/admin/migrations`` for one service."""

    url = _service_url(cli_ctx, service)
    with authed_client(url, cli_ctx.config.api_key, timeout=cli_ctx.config.timeout) as client:
        code, body = get_json(client, "/v1/admin/migrations")
    if code == -1:
        return {
            "service": service,
            "url": url,
            "ok": False,
            "error": (body.get("error") if isinstance(body, dict) else str(body)),
        }
    if code != 200:
        return {
            "service": service,
            "url": url,
            "ok": False,
            "status_code": code,
            "body": body,
        }
    applied = (body.get("applied") if isinstance(body, dict) else None) or []
    pending = (body.get("pending") if isinstance(body, dict) else None) or []
    return {
        "service": service,
        "url": url,
        "ok": True,
        "applied_count": len(applied),
        "pending_count": len(pending),
        "applied": [_short_id(m) for m in applied],
        "pending": [_short_id(m) for m in pending],
    }


def _short_id(m: Any) -> str:
    """Pull a short id from various server response shapes."""

    if isinstance(m, str):
        return m
    if isinstance(m, dict):
        return str(m.get("id") or m.get("name") or m.get("label") or m)
    return str(m)


def _emit_status_table(rows: list[dict[str, Any]]) -> None:
    """Render the status payload as a human-friendly table."""

    table_rows: list[list[str]] = []
    for r in rows:
        if not r["ok"]:
            table_rows.append([
                r["service"],
                "[red]ERROR[/]",
                "",
                "",
                r.get("error") or f"HTTP {r.get('status_code')}",
            ])
            continue
        table_rows.append([
            r["service"],
            "[green]ok[/]",
            str(r["applied_count"]),
            str(r["pending_count"]),
            ", ".join(r["pending"]) if r["pending"] else "—",
        ])
    emit_table(
        "Migration status",
        ["Service", "Status", "Applied", "Pending", "Pending IDs"],
        table_rows,
    )


@group.command("apply", help="Apply pending migrations for a service.")
@click.argument("service")
@click.option("--to", "to_id", default=None, help="Apply forward up to this migration id.")
@click.pass_context
def apply(ctx: click.Context, service: str, to_id: str | None) -> None:
    """POST ``/v1/admin/migrations/apply`` and surface the response."""

    cli_ctx: CLIContext = ctx.obj
    url = _service_url(cli_ctx, service)
    body: dict[str, Any] = {}
    if to_id:
        body["to"] = to_id
    with authed_client(url, cli_ctx.config.api_key, timeout=cli_ctx.config.timeout) as client:
        code, payload = post_json(client, "/v1/admin/migrations/apply", json=body)
    _emit_action_result(cli_ctx, "apply", service, code, payload)


@group.command(
    "rollback-to",
    help="Roll back applied migrations down to (and including) <id>.",
)
@click.argument("service")
@click.argument("target_id")
@click.option("--dry-run", is_flag=True, default=False, help="Print the plan without executing.")
@click.pass_context
def rollback_to(
    ctx: click.Context,
    service: str,
    target_id: str,
    dry_run: bool,
) -> None:
    """POST ``/v1/admin/migrations/rollback`` with the requested target."""

    cli_ctx: CLIContext = ctx.obj
    url = _service_url(cli_ctx, service)
    body: dict[str, Any] = {"to": target_id, "dry_run": dry_run}
    with authed_client(url, cli_ctx.config.api_key, timeout=cli_ctx.config.timeout) as client:
        code, payload = post_json(client, "/v1/admin/migrations/rollback", json=body)
    _emit_action_result(cli_ctx, "rollback-to", service, code, payload)


@group.command("create", help="Scaffold a new migration file (service-side only).")
@click.argument("service")
@click.argument("label")
@click.pass_context
def create(ctx: click.Context, service: str, label: str) -> None:
    """Surface a friendly message — server doesn't expose a scaffolding API.

    The actual scaffold lives in each service's ``__main__`` (e.g.
    ``python -m plinth_workspace migrate --create <label>``); creating a
    file via HTTP would defeat the point of code-reviewed migrations.
    """

    if service not in _SERVICE_TO_FIELD:
        raise click.ClickException(
            f"unknown service {service!r}. known: workspace, gateway, identity"
        )
    package = {
        "workspace": "plinth_workspace",
        "gateway": "plinth_gateway",
        "identity": "plinth_identity",
    }[service]
    cli_ctx: CLIContext = ctx.obj
    if cli_ctx.output_mode() == "json":
        emit_json({
            "service": service,
            "scaffold_via": f"python -m {package} migrate --create {label!r}",
        })
        return
    emit_human(
        f"Scaffolding new migration files is a code change — run "
        f"[bold]python -m {package} migrate --create '{label}'[/] from the "
        f"service checkout to add a versioned SQL file."
    )


def _emit_action_result(
    cli_ctx: CLIContext,
    action: str,
    service: str,
    status_code: int,
    payload: Any,
) -> None:
    """Common formatting path for apply/rollback responses."""

    ok = 200 <= status_code < 300
    if cli_ctx.output_mode() == "json":
        emit_json({
            "service": service,
            "action": action,
            "ok": ok,
            "status_code": status_code,
            "body": payload,
        })
        if not ok:
            # Surface the HTTP error to the shell. Click swallows the
            # ClickException's stderr message after we've already printed
            # the JSON payload, but the non-zero exit propagates.
            raise click.ClickException(
                f"{service} {action} failed (HTTP {status_code})"
            )
        return
    if not ok:
        raise click.ClickException(
            f"{service} {action} failed (HTTP {status_code}): {payload}"
        )
    emit_human(f"[green]{service}[/] {action} ok: {payload}")


@group.command("all", help="Show migration status across every service (alias for `status all`).")
@click.option("--status", "status_only", is_flag=True, default=True, help="Show status only.")
@click.pass_context
def all_(ctx: click.Context, status_only: bool) -> None:
    """Compatibility alias matching ``plinth migrate all --status``."""

    ctx.invoke(status, service="all")


__all__ = ["group"]
