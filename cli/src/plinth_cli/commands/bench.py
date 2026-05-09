# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""``plinth bench`` — thin proxy over the ``plinth-bench`` harness.

The benchmark suite has its own console script (``plinth-bench``) that
ships in ``benchmarks/``. We don't reimplement load generation here; the
CLI just delegates to that binary so users have a uniform entrypoint.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import click

from ..output import emit_human, emit_json


def _bench_binary() -> str | None:
    """Locate the ``plinth-bench`` executable on PATH or in the venv."""

    return shutil.which("plinth-bench")


@click.group(help="Run + compare benchmark suites (delegates to plinth-bench).")
def group() -> None:
    """Container for the ``bench`` subcommands."""


@group.command("quick", help="Quick sanity benchmark (target_rps=100, hold=10s).")
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to write run results.",
)
@click.pass_context
def quick(ctx: click.Context, output_dir: Path | None) -> None:
    """Run a short load test for smoke purposes."""

    args = [
        "all",
        "--target-rps",
        "100",
        "--hold-seconds",
        "10",
        "--ramp-seconds",
        "5",
        "--cooldown-seconds",
        "2",
    ]
    if output_dir:
        args += ["--output-dir", str(output_dir)]
    _run_bench(ctx, args)


@group.command("full", help="Full benchmark suite (~15 min on local).")
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to write run results.",
)
@click.pass_context
def full(ctx: click.Context, output_dir: Path | None) -> None:
    """Run the standard suite (no overrides)."""

    args = ["all"]
    if output_dir:
        args += ["--output-dir", str(output_dir)]
    _run_bench(ctx, args)


@group.command("compare", help="Compare two run JSONs (baseline + latest).")
@click.argument("baseline", type=click.Path(exists=True, dir_okay=False))
@click.argument("latest", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def compare(ctx: click.Context, baseline: str, latest: str) -> None:
    """Pass through to ``plinth-bench compare``."""

    _run_bench(ctx, ["compare", baseline, latest])


def _run_bench(ctx: click.Context, args: list[str]) -> None:
    """Execute the bench binary with ``args`` and stream output."""

    bin_path = _bench_binary()
    if bin_path is None:
        from ..main import CLIContext

        cli_ctx: CLIContext = ctx.obj
        message = (
            "plinth-bench is not installed. Install via "
            "`pip install -e ./benchmarks` from the repo root."
        )
        if cli_ctx.output_mode() == "json":
            emit_json({"ok": False, "error": message})
        else:
            emit_human(f"[red]error:[/] {message}")
        ctx.exit(127)
        return

    # We exec to keep stdout interactive (rich progress bars survive).
    cmd = [bin_path, *args]
    if os.name != "nt":
        os.execvp(cmd[0], cmd)
    else:  # pragma: no cover - macOS/Linux only in our CI
        proc = subprocess.run(cmd, check=False)
        ctx.exit(proc.returncode)


__all__ = ["group"]
