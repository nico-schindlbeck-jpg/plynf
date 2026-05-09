# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""``plinth services`` — start/stop/status/logs for backing services.

Mirrors the Makefile's ``serve``/``stop``/healthcheck flow but as a single
ergonomic CLI. Process lifecycle goes through ``scripts/_spawn.py`` so the
CLI and the Makefile share one start path; logs land in ``/tmp/plinth-logs``
and PIDs in ``/tmp/plinth-pids`` — same conventions.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import click
import httpx

from .. import settings as _s
from ..main import CLIContext
from ..output import emit_human, emit_json, emit_table

SPAWN_SCRIPT_DEFAULT = Path("/Users/nico/Code/plinth/scripts/_spawn.py")


def _find_spawn_script() -> Path | None:
    """Locate ``scripts/_spawn.py`` if running inside the monorepo.

    Falls back to ``None`` for installs outside the source tree (the CLI
    will then surface a clear "not bundled" error rather than crash).
    """

    env_override = os.environ.get("PLINTH_SPAWN_SCRIPT")
    if env_override:
        p = Path(env_override)
        if p.exists():
            return p
    if SPAWN_SCRIPT_DEFAULT.exists():
        return SPAWN_SCRIPT_DEFAULT
    # Walk up from cwd looking for scripts/_spawn.py — handy for forks/checkouts.
    here = Path.cwd()
    for parent in [here, *here.parents]:
        cand = parent / "scripts" / "_spawn.py"
        if cand.exists():
            return cand
    return None


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group(help="Manage backing services (workspace, gateway, identity, …).")
def group() -> None:
    """Container for the ``services`` subcommands."""


@group.command("start", help="Start one or all services in the background.")
@click.argument("name", default="all")
@click.option(
    "--data-dir",
    default=str(_s.DEFAULT_DATA_DIR),
    show_default=True,
    help="Where services persist state.",
)
@click.pass_context
def start(ctx: click.Context, name: str, data_dir: str) -> None:
    """Spawn ``name`` (or every service when ``all``)."""

    cli_ctx: CLIContext = ctx.obj
    targets = _expand_targets(name)
    spawn = _find_spawn_script()
    if spawn is None:
        raise click.ClickException(
            "scripts/_spawn.py not found — run from a Plinth checkout or "
            "set PLINTH_SPAWN_SCRIPT=/path/to/_spawn.py."
        )

    _s.LOG_DIR.mkdir(parents=True, exist_ok=True)
    _s.PID_DIR.mkdir(parents=True, exist_ok=True)

    rows: list[list[str]] = []
    for svc, _url, package, port_env in targets:
        if package is None:  # mock-mcp etc may share their own module
            continue
        result = _start_one(svc, package, port_env, data_dir=data_dir, spawn=spawn)
        rows.append([svc, result])

    if cli_ctx.output_mode() == "json":
        emit_json([{"service": r[0], "result": r[1]} for r in rows])
        return
    emit_table("Services started", ["Service", "Status"], rows)


def _start_one(
    name: str,
    package: str,
    port_env: str,
    *,
    data_dir: str,
    spawn: Path,
) -> str:
    """Spawn one service and return a short status string."""

    pid_file = _s.PID_DIR / f"{name}.pid"
    log_file = _s.LOG_DIR / f"{name}.log"

    if pid_file.exists():
        try:
            existing = int(pid_file.read_text().strip())
            os.kill(existing, 0)  # raises if dead
            return f"already running (pid {existing})"
        except (OSError, ValueError):
            pid_file.unlink(missing_ok=True)

    env_pairs = [
        f"PLINTH_DATA_DIR={data_dir}",
        f"PLINTH_IDENTITY_DATA_DIR={data_dir}",
        f"{port_env}={_default_port(port_env)}",
    ]
    cmd = [
        sys.executable,
        str(spawn),
        str(pid_file),
        str(log_file),
        *env_pairs,
        "--",
        sys.executable,
        "-m",
        package,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        return f"failed to spawn ({exc})"
    if proc.returncode != 0:
        return f"failed (exit {proc.returncode}: {proc.stderr.strip()[:80]})"
    pid = proc.stdout.strip() or "?"
    return f"started (pid {pid})"


def _default_port(env_name: str) -> str:
    """Look up the default port for ``env_name`` from :mod:`settings`."""

    for _name, url, _pkg, env in _s.SERVICES:
        if env == env_name:
            return url.rsplit(":", 1)[-1]
    return "0"


@group.command("stop", help="Stop one or all services.")
@click.argument("name", default="all")
@click.pass_context
def stop(ctx: click.Context, name: str) -> None:
    """SIGTERM the recorded PID(s) and remove the pid file."""

    cli_ctx: CLIContext = ctx.obj
    targets = _expand_targets(name)

    rows: list[list[str]] = []
    for svc, *_ in targets:
        result = _stop_one(svc)
        rows.append([svc, result])

    if cli_ctx.output_mode() == "json":
        emit_json([{"service": r[0], "result": r[1]} for r in rows])
        return
    emit_table("Services stopped", ["Service", "Status"], rows)


def _stop_one(name: str) -> str:
    pid_file = _s.PID_DIR / f"{name}.pid"
    if not pid_file.exists():
        return "not running"
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        pid_file.unlink(missing_ok=True)
        return "not running (stale pid file removed)"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        return f"already stopped (stale pid {pid})"
    pid_file.unlink(missing_ok=True)
    return f"stopped (pid {pid})"


@group.command("status", help="Show PID + healthz status for every service.")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print a status table; non-zero exit when any service is failing."""

    cli_ctx: CLIContext = ctx.obj
    cfg = cli_ctx.config

    rows: list[list[str]] = []
    json_rows: list[dict[str, str | int | bool | None]] = []
    overrides = {
        "workspace": cfg.workspace_url,
        "gateway": cfg.gateway_url,
        "identity": cfg.identity_url,
        "dashboard": cfg.dashboard_url,
    }
    bad = False
    for svc, default_url, _pkg, _env in _s.SERVICES:
        pid = _read_pid(svc)
        url = overrides.get(svc, default_url)
        ok = _ping_health(url, timeout=cfg.timeout)
        if pid is None:
            pid_label = "—"
        else:
            pid_label = str(pid)
        rows.append(
            [
                svc,
                pid_label,
                "✔" if ok else "✘",
                url,
            ]
        )
        json_rows.append({"service": svc, "pid": pid, "ok": ok, "url": url})
        if not ok:
            bad = True

    if cli_ctx.output_mode() == "json":
        emit_json(json_rows)
    else:
        emit_table("Services", ["Service", "PID", "Health", "URL"], rows)

    if bad:
        ctx.exit(1)


def _read_pid(name: str) -> int | None:
    pid_file = _s.PID_DIR / f"{name}.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _ping_health(base_url: str, *, timeout: float) -> bool:
    target = base_url.rstrip("/") + "/healthz"
    try:
        with httpx.Client(timeout=min(timeout, 3.0)) as client:
            resp = client.get(target)
        return 200 <= resp.status_code < 300
    except httpx.HTTPError:
        return False


@group.command("logs", help="Tail the log file for a service.")
@click.argument("name")
@click.option("--tail", "tail_n", default=50, show_default=True, help="Lines to show from EOF.")
@click.option("-f", "--follow", is_flag=True, default=False, help="Stream new lines (tail -f).")
@click.pass_context
def logs(ctx: click.Context, name: str, tail_n: int, follow: bool) -> None:
    """Print the last ``--tail`` lines of ``<name>.log``; ``-f`` to stream."""

    if name == "all":
        raise click.ClickException("logs needs a single service name (try `services status`).")
    if name not in _s.service_names():
        raise click.ClickException(
            f"unknown service {name!r}. known: {', '.join(_s.service_names())}"
        )

    log_file = _s.LOG_DIR / f"{name}.log"
    if not log_file.exists():
        raise click.ClickException(f"no log file at {log_file} (service may not have run)")

    if follow:
        # Defer to system ``tail`` if present — far more efficient than polling.
        tail_bin = shutil.which("tail")
        if tail_bin:
            os.execv(tail_bin, [tail_bin, "-n", str(tail_n), "-f", str(log_file)])
        # Fallback: read once.
        emit_human(_last_lines(log_file, tail_n))
        return
    emit_human(_last_lines(log_file, tail_n))


def _last_lines(path: Path, n: int) -> str:
    """Return the last ``n`` lines of ``path``."""

    text = path.read_text(errors="replace")
    lines = text.splitlines()
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expand_targets(name: str) -> list[tuple[str, str, str, str]]:
    """Return either the single service tuple or every registered one."""

    if name == "all":
        return list(_s.SERVICES)
    for entry in _s.SERVICES:
        if entry[0] == name:
            return [entry]
    raise click.ClickException(
        f"unknown service {name!r}. known: {', '.join(_s.service_names())}"
    )
