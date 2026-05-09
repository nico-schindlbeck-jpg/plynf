# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""``plinth health`` — cross-service ``/healthz`` aggregation."""

from __future__ import annotations

import time
from typing import Any

import click
import httpx

from .. import settings as _s
from ..main import CLIContext
from ..output import emit_human, emit_json


@click.group(
    invoke_without_command=True,
    help="Hit /healthz on every Plinth service and print a status table.",
)
@click.pass_context
def group(ctx: click.Context) -> None:
    """Default to the ``health`` (no-arg) action."""

    if ctx.invoked_subcommand is None:
        run_health(ctx)


@group.command("watch", help="Re-poll /healthz every 2 seconds (Ctrl-C to exit).")
@click.option("--interval", "interval", default=2.0, show_default=True, help="Poll interval (s).")
@click.pass_context
def watch(ctx: click.Context, interval: float) -> None:
    """Block printing one health table per ``interval``."""

    cli_ctx: CLIContext = ctx.obj
    try:
        while True:
            click.clear()
            _emit(_collect(cli_ctx), mode=cli_ctx.output_mode())
            time.sleep(max(0.5, float(interval)))
    except KeyboardInterrupt:
        return


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def run_health(ctx: click.Context) -> None:
    """Single-shot health table (default for ``plinth health``)."""

    cli_ctx: CLIContext = ctx.obj
    rows = _collect(cli_ctx)
    _emit(rows, mode=cli_ctx.output_mode())
    if any(not r["ok"] for r in rows):
        ctx.exit(1)


def _collect(cli_ctx: CLIContext) -> list[dict[str, Any]]:
    """Probe every known service's ``/healthz`` endpoint."""

    cfg = cli_ctx.config
    overrides = {
        "workspace": cfg.workspace_url,
        "gateway": cfg.gateway_url,
        "identity": cfg.identity_url,
        "dashboard": cfg.dashboard_url,
    }

    out: list[dict[str, Any]] = []
    for name, default_url, _pkg, _env in _s.SERVICES:
        url = overrides.get(name, default_url)
        out.append(_probe(name, url, timeout=cfg.timeout))
    return out


def _probe(name: str, base_url: str, *, timeout: float) -> dict[str, Any]:
    """Query ``<base_url>/healthz`` and return a row dict."""

    target = base_url.rstrip("/") + "/healthz"
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=min(timeout, 5.0)) as client:
            resp = client.get(target)
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text}
        return {
            "name": name,
            "url": target,
            "ok": 200 <= resp.status_code < 300,
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
            "version": _extract(payload, "version"),
            "uptime": _extract(payload, "uptime"),
            "payload": payload,
        }
    except httpx.HTTPError as exc:
        return {
            "name": name,
            "url": target,
            "ok": False,
            "status_code": None,
            "latency_ms": None,
            "version": None,
            "uptime": None,
            "error": str(exc),
        }


def _extract(payload: Any, key: str) -> Any | None:
    """Best-effort dict lookup that tolerates non-dict payloads."""

    if isinstance(payload, dict):
        return payload.get(key)
    return None


def _emit(rows: list[dict[str, Any]], *, mode: str) -> None:
    """Render the collected rows."""

    if mode == "json":
        emit_json({r["name"]: _strip(r) for r in rows})
        return

    ok_count = sum(1 for r in rows if r["ok"])
    fail_count = len(rows) - ok_count

    lines = ["[bold]Plinth health[/]", "─────────────"]
    for r in rows:
        marker = "[green]✔[/]" if r["ok"] else "[red]✘[/]"
        url = r["url"]
        if r["ok"]:
            extra_bits: list[str] = []
            if r.get("version"):
                extra_bits.append(f"v={r['version']}")
            if r.get("uptime") is not None:
                extra_bits.append(f"uptime={r['uptime']}")
            if r.get("latency_ms") is not None:
                extra_bits.append(f"latency={r['latency_ms']}ms")
            extra = "  " + "  ".join(extra_bits) if extra_bits else ""
            lines.append(f"  {marker} {r['name']:<11} {url}{extra}")
        else:
            err = r.get("error") or f"HTTP {r.get('status_code')}"
            lines.append(f"  {marker} {r['name']:<11} {url}   [red]{err}[/]")
    lines.append("─────────────")
    summary = f"  [bold]{ok_count}[/] ok, "
    summary += f"[red]{fail_count}[/] failing" if fail_count else "[green]0[/] failing"
    lines.append(summary)
    emit_human("\n".join(lines))


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    """Drop the bulky raw payload from JSON output."""

    return {k: v for k, v in row.items() if k != "payload"}
