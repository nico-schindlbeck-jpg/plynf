# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""``plinth tenant`` — administer tenants on the identity service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from .._http import authed_client, get_json, post_json
from ..main import CLIContext
from ..output import emit_human, emit_json, emit_kv, emit_table


@click.group(help="Administer tenants (identity + workspace + gateway).")
def group() -> None:
    """Container for the ``tenant`` subcommands."""


# ---------------------------------------------------------------------------
# list / show
# ---------------------------------------------------------------------------


@group.command("list", help="List tenants known to the identity service.")
@click.pass_context
def list_tenants(ctx: click.Context) -> None:
    """GET ``/v1/tenants`` from identity and render."""

    cli_ctx: CLIContext = ctx.obj
    cfg = cli_ctx.config
    with authed_client(cfg.identity_url, cfg.api_key, timeout=cfg.timeout) as client:
        code, body = get_json(client, "/v1/tenants")
    if code != 200 or not isinstance(body, dict):
        raise click.ClickException(f"list failed (HTTP {code}): {body}")
    tenants = body.get("tenants", []) or []
    if cli_ctx.output_mode() == "json":
        emit_json(tenants)
        return
    rows = [
        [
            t.get("id", ""),
            t.get("name", ""),
            t.get("created_at") or "—",
            ", ".join(f"{k}={v}" for k, v in (t.get("metadata") or {}).items()) or "—",
        ]
        for t in tenants
    ]
    emit_table(f"Tenants ({len(rows)})", ["ID", "Name", "Created", "Metadata"], rows)


@group.command("show", help="Show full details for a single tenant.")
@click.argument("tenant_id")
@click.pass_context
def show(ctx: click.Context, tenant_id: str) -> None:
    """GET ``/v1/tenants/<id>`` and render."""

    cli_ctx: CLIContext = ctx.obj
    cfg = cli_ctx.config
    with authed_client(cfg.identity_url, cfg.api_key, timeout=cfg.timeout) as client:
        code, body = get_json(client, f"/v1/tenants/{tenant_id}")
    if code != 200 or not isinstance(body, dict):
        raise click.ClickException(f"show failed (HTTP {code}): {body}")
    if cli_ctx.output_mode() == "json":
        emit_json(body)
        return
    flat = {k: v for k, v in body.items() if not isinstance(v, (dict, list))}
    emit_kv(f"Tenant — {tenant_id}", flat)
    if isinstance(body.get("metadata"), dict):
        emit_kv("Metadata", body["metadata"])


# ---------------------------------------------------------------------------
# create / quotas / usage
# ---------------------------------------------------------------------------


@group.command("create", help="Create a new tenant.")
@click.argument("tenant_id")
@click.option("--name", required=True, help="Human-readable tenant name.")
@click.option("--metadata", "metadata_pairs", multiple=True, help="key=value (repeatable).")
@click.pass_context
def create(
    ctx: click.Context,
    tenant_id: str,
    name: str,
    metadata_pairs: tuple[str, ...],
) -> None:
    """POST ``/v1/tenants`` with the requested body."""

    cli_ctx: CLIContext = ctx.obj
    cfg = cli_ctx.config
    metadata = _parse_kvs(metadata_pairs)
    with authed_client(cfg.identity_url, cfg.api_key, timeout=cfg.timeout) as client:
        code, body = post_json(
            client,
            "/v1/tenants",
            json={"id": tenant_id, "name": name, "metadata": metadata},
        )
    if not 200 <= code < 300:
        raise click.ClickException(f"create failed (HTTP {code}): {body}")
    if cli_ctx.output_mode() == "json":
        emit_json(body)
        return
    emit_human(f"[green]created[/] tenant {tenant_id}")


@group.command("quotas", help="Show or update per-tenant quotas.")
@click.argument("tenant_id")
@click.option(
    "--set",
    "set_pairs",
    multiple=True,
    metavar="KEY=VAL",
    help="Update one or more quota fields (e.g. --set max_workspaces=200).",
)
@click.pass_context
def quotas(ctx: click.Context, tenant_id: str, set_pairs: tuple[str, ...]) -> None:
    """GET (or POST when ``--set`` is provided) the tenant quotas endpoint."""

    cli_ctx: CLIContext = ctx.obj
    cfg = cli_ctx.config
    path = f"/v1/tenants/{tenant_id}/quotas"
    with authed_client(cfg.identity_url, cfg.api_key, timeout=cfg.timeout) as client:
        if set_pairs:
            update = _parse_kvs(set_pairs, coerce_numeric=True)
            code, body = post_json(client, path, json=update)
        else:
            code, body = get_json(client, path)
    if not 200 <= code < 300:
        raise click.ClickException(f"quotas request failed (HTTP {code}): {body}")
    if cli_ctx.output_mode() == "json":
        emit_json(body)
        return
    if isinstance(body, dict):
        emit_kv(f"Quotas — {tenant_id}", body)
    else:
        emit_human(str(body))


@group.command("usage", help="Show current usage rollup for a tenant.")
@click.argument("tenant_id")
@click.pass_context
def usage(ctx: click.Context, tenant_id: str) -> None:
    """GET ``/v1/tenants/<id>/usage`` and render."""

    cli_ctx: CLIContext = ctx.obj
    cfg = cli_ctx.config
    path = f"/v1/tenants/{tenant_id}/usage"
    with authed_client(cfg.identity_url, cfg.api_key, timeout=cfg.timeout) as client:
        code, body = get_json(client, path)
    if not 200 <= code < 300:
        raise click.ClickException(f"usage request failed (HTTP {code}): {body}")
    if cli_ctx.output_mode() == "json":
        emit_json(body)
        return
    if isinstance(body, dict):
        emit_kv(f"Usage — {tenant_id}", body)
    else:
        emit_human(str(body))


# ---------------------------------------------------------------------------
# export / delete (compliance scaffolding)
# ---------------------------------------------------------------------------


@group.command("export", help="GDPR export — kicks off /export and returns the export id.")
@click.argument("tenant_id")
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="If supplied, the path to write the JSON receipt to.",
)
@click.pass_context
def export(ctx: click.Context, tenant_id: str, output_path: Path | None) -> None:
    """POST ``/v1/tenants/<id>/export`` and surface the response."""

    cli_ctx: CLIContext = ctx.obj
    cfg = cli_ctx.config
    with authed_client(cfg.identity_url, cfg.api_key, timeout=cfg.timeout) as client:
        code, body = post_json(client, f"/v1/tenants/{tenant_id}/export")
    if not 200 <= code < 300:
        raise click.ClickException(f"export failed (HTTP {code}): {body}")
    if output_path is not None:
        import json as _json

        output_path.write_text(_json.dumps(body, indent=2, default=str))
        emit_human(f"[green]wrote[/] {output_path}")
    if cli_ctx.output_mode() == "json":
        emit_json(body)
        return
    emit_kv(f"Export — {tenant_id}", body if isinstance(body, dict) else {"raw": body})


@group.command("delete", help="GDPR-style hard delete of a tenant (requires confirm token).")
@click.argument("tenant_id")
@click.option("--confirm", "confirm_token", required=True, help="Two-phase confirm token.")
@click.pass_context
def delete(ctx: click.Context, tenant_id: str, confirm_token: str) -> None:
    """DELETE ``/v1/tenants/<id>/data?confirm=<token>``."""

    cli_ctx: CLIContext = ctx.obj
    cfg = cli_ctx.config
    with authed_client(cfg.identity_url, cfg.api_key, timeout=cfg.timeout) as client:
        try:
            resp = client.delete(
                f"/v1/tenants/{tenant_id}/data",
                params={"confirm": confirm_token},
            )
        except Exception as exc:  # broad: surface transport errors as cli errors
            raise click.ClickException(f"delete failed: {exc}") from exc
    code = resp.status_code
    try:
        body: Any = resp.json()
    except ValueError:
        body = {"text": resp.text}
    if not 200 <= code < 300:
        raise click.ClickException(f"delete failed (HTTP {code}): {body}")
    if cli_ctx.output_mode() == "json":
        emit_json(body)
        return
    emit_human(f"[green]delete accepted[/] for {tenant_id}: {body}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_kvs(pairs: tuple[str, ...], *, coerce_numeric: bool = False) -> dict[str, Any]:
    """Parse ``key=value`` pairs into a dict (optionally coercing numerics)."""

    out: dict[str, Any] = {}
    for raw in pairs:
        if "=" not in raw:
            raise click.ClickException(f"expected key=value, got: {raw!r}")
        key, _, val = raw.partition("=")
        if coerce_numeric:
            out[key] = _maybe_number(val)
        else:
            out[key] = val
    return out


def _maybe_number(raw: str) -> Any:
    """Best-effort coerce string to int/float; fall back to the original."""

    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


__all__ = ["group"]
