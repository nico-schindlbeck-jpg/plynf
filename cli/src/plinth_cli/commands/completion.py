# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""``plinth completion`` — generate or install shell completion.

Click ships a battle-tested completion mechanism out of the box; we just
expose it under a friendlier name + write the script into the user's
shell rc on demand.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import click

from ..output import emit_human

_SHELLS = ("bash", "zsh", "fish")


def _detect_shell() -> str:
    """Return the user's shell from $SHELL (or ``"bash"`` as a safe default)."""

    raw = os.environ.get("SHELL", "")
    base = os.path.basename(raw)
    if base in _SHELLS:
        return base
    return "bash"


@click.group(help="Print or install shell completion scripts.")
def group() -> None:
    """Container for the ``completion`` subcommands."""


@group.command("show", help="Print the completion script for SHELL.")
@click.option(
    "--shell",
    type=click.Choice(_SHELLS, case_sensitive=False),
    default=None,
    help="Target shell (auto-detected from $SHELL).",
)
def show(shell: str | None) -> None:
    """Run ``_PLINTH_COMPLETE=<shell>_source plinth`` and print stdout."""

    script = _generate_script((shell or _detect_shell()).lower())
    click.echo(script)


@group.command(
    "install",
    help="Append the completion script to a shell rc file (with idempotence guard).",
)
@click.option(
    "--shell",
    type=click.Choice(_SHELLS, case_sensitive=False),
    default=None,
    help="Target shell (auto-detected from $SHELL).",
)
@click.option(
    "--rc-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Override the rc file (default: shell-dependent).",
)
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing block.")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the planned snippet + target rc path without writing.",
)
def install(
    shell: str | None,
    rc_file: Path | None,
    force: bool,
    dry_run: bool,
) -> None:
    """Write the completion snippet into the user's rc file."""

    target_shell = (shell or _detect_shell() or "bash").lower()
    rc = rc_file if rc_file is not None else _default_rc(target_shell)

    marker_start = "# >>> plinth completion >>>"
    marker_end = "# <<< plinth completion <<<"
    block = (
        f"{marker_start}\n"
        f'eval "$(_PLINTH_COMPLETE={target_shell}_source plinth)"\n'
        f"{marker_end}\n"
    )

    if dry_run:
        # Side-effect-free path: useful for the verification smoke
        # test in the spec, plus operators that want to inspect what
        # the install would do before letting it touch their rc file.
        emit_human(f"[bold]target shell:[/] {target_shell}")
        emit_human(f"[bold]target rc:[/]    {rc}")
        emit_human("[bold]snippet:[/]")
        emit_human(block.rstrip())
        return

    rc.parent.mkdir(parents=True, exist_ok=True)
    existing = rc.read_text() if rc.exists() else ""
    if marker_start in existing and not force:
        emit_human(
            f"completion block already present in {rc}. Re-run with --force to overwrite."
        )
        return

    if marker_start in existing and force:
        existing = _strip_block(existing, marker_start, marker_end)

    rc.write_text(existing.rstrip() + "\n\n" + block)
    emit_human(f"[green]installed[/] completion in {rc}.")
    emit_human(f"Open a new shell or run: [bold]source {rc}[/]")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _generate_script(shell: str) -> str:
    """Invoke the ``_PLINTH_COMPLETE`` source mode and capture stdout.

    Tries the ``plinth`` console script first, then falls back to
    ``python -m plinth_cli`` so the command works inside a venv whose
    ``bin/`` is not on PATH (a common CI / Makefile setup).
    """

    import sys as _sys

    env = os.environ.copy()
    env["_PLINTH_COMPLETE"] = f"{shell}_source"
    candidates = [["plinth"], [_sys.executable, "-m", "plinth_cli"]]
    last_err: Exception | None = None
    for cmd in candidates:
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                check=False,
                text=True,
            )
        except (FileNotFoundError, OSError) as exc:
            last_err = exc
            continue
        if proc.stdout.strip():
            return proc.stdout
        # No output but no exception → keep trying the next candidate.
        last_err = RuntimeError(proc.stderr.strip() or "no output")
    raise click.ClickException(
        f"completion generation failed: {last_err}"
    )


def _default_rc(shell: str) -> Path:
    """Return the conventional rc file location for ``shell``."""

    home = Path.home()
    return {
        "bash": home / ".bashrc",
        "zsh": home / ".zshrc",
        "fish": home / ".config" / "fish" / "completions" / "plinth.fish",
    }.get(shell, home / ".bashrc")


def _strip_block(text: str, start: str, end: str) -> str:
    """Remove the existing completion block (between markers) from ``text``."""

    out: list[str] = []
    skip = False
    for line in text.splitlines():
        if line.strip() == start:
            skip = True
            continue
        if skip and line.strip() == end:
            skip = False
            continue
        if not skip:
            out.append(line)
    return "\n".join(out)


__all__ = ["group"]
