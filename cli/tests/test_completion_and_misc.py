# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for ``plinth completion`` plus other small odds-and-ends."""

from __future__ import annotations

from pathlib import Path

import pytest

from plinth_cli import settings as _s
from plinth_cli.commands.bench import _bench_binary
from plinth_cli.commands.completion import _default_rc, _detect_shell, _strip_block
from plinth_cli.main import cli

# ---------------------------------------------------------------------------
# Helper coverage
# ---------------------------------------------------------------------------


def test_settings_service_names_contains_workspace() -> None:
    names = _s.service_names()
    assert "workspace" in names
    assert "gateway" in names
    assert "identity" in names


def test_default_rc_per_shell() -> None:
    assert _default_rc("bash").name == ".bashrc"
    assert _default_rc("zsh").name == ".zshrc"
    assert "fish" in str(_default_rc("fish"))


def test_detect_shell_uses_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert _detect_shell() == "zsh"


def test_detect_shell_falls_back_to_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/whatever")
    assert _detect_shell() == "bash"


def test_strip_block_removes_marked_section() -> None:
    text = (
        "first\n"
        "# >>> plinth completion >>>\n"
        "old\n"
        "# <<< plinth completion <<<\n"
        "after\n"
    )
    out = _strip_block(text, "# >>> plinth completion >>>", "# <<< plinth completion <<<")
    assert "old" not in out
    assert "first" in out and "after" in out


def test_bench_binary_returns_string_or_none() -> None:
    # We don't make assertions about the actual binary — only that the
    # helper returns a sensible type without raising.
    assert _bench_binary() is None or isinstance(_bench_binary(), str)


# ---------------------------------------------------------------------------
# CLI tests for ``plinth completion install``
# ---------------------------------------------------------------------------


def test_completion_install_writes_block(runner, tmp_path: Path) -> None:
    """Installing into a fresh rc file appends the marker block."""

    rc = tmp_path / "rc"
    result = runner.invoke(
        cli,
        ["completion", "install", "--shell", "bash", "--rc-file", str(rc)],
    )
    assert result.exit_code == 0, result.output
    text = rc.read_text()
    assert "# >>> plinth completion >>>" in text
    assert "_PLINTH_COMPLETE=bash_source" in text


def test_completion_install_idempotent(runner, tmp_path: Path) -> None:
    """A second install without ``--force`` leaves the file alone."""

    rc = tmp_path / "rc"
    runner.invoke(
        cli,
        ["completion", "install", "--shell", "bash", "--rc-file", str(rc)],
    )
    before = rc.read_text()
    runner.invoke(
        cli,
        ["completion", "install", "--shell", "bash", "--rc-file", str(rc)],
    )
    assert rc.read_text() == before


def test_completion_install_force_replaces(runner, tmp_path: Path) -> None:
    """``--force`` rewrites the existing block (no duplicates)."""

    rc = tmp_path / "rc"
    runner.invoke(
        cli,
        ["completion", "install", "--shell", "bash", "--rc-file", str(rc)],
    )
    runner.invoke(
        cli,
        ["completion", "install", "--shell", "zsh", "--rc-file", str(rc), "--force"],
    )
    text = rc.read_text()
    assert text.count("# >>> plinth completion >>>") == 1
    assert "_PLINTH_COMPLETE=zsh_source" in text


# ---------------------------------------------------------------------------
# Bench command (no plinth-bench installed in test env)
# ---------------------------------------------------------------------------


def test_bench_quick_without_binary(
    runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``plinth-bench`` isn't on PATH, ``bench quick`` exits non-zero."""

    monkeypatch.setattr("plinth_cli.commands.bench._bench_binary", lambda: None)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[default]\nworkspace_url = "http://x.test"\n')
    result = runner.invoke(cli, ["--config", str(cfg), "--json", "bench", "quick"])
    assert result.exit_code != 0


def test_completion_install_dry_run_no_writes(runner, tmp_path: Path) -> None:
    """``--dry-run`` prints the planned snippet without writing the rc file."""

    rc = tmp_path / "rc.does.not.exist"
    result = runner.invoke(
        cli,
        [
            "completion",
            "install",
            "--shell",
            "bash",
            "--rc-file",
            str(rc),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not rc.exists()
    assert "_PLINTH_COMPLETE=bash_source" in result.output


def test_completion_install_dry_run_zsh(runner, tmp_path: Path) -> None:
    """Dry-run picks up the requested shell."""

    rc = tmp_path / "rc.zsh"
    result = runner.invoke(
        cli,
        [
            "completion",
            "install",
            "--shell",
            "zsh",
            "--rc-file",
            str(rc),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "_PLINTH_COMPLETE=zsh_source" in result.output


def test_completion_install_dry_run_default_rc_unchanged(
    runner, tmp_path: Path
) -> None:
    """Dry-run reports the *would-be* default rc path without touching it."""

    # We don't pass ``--rc-file``; the command should print whatever
    # ``_default_rc`` returns (e.g. ``$HOME/.bashrc``) and leave the
    # filesystem alone.
    result = runner.invoke(
        cli,
        ["completion", "install", "--shell", "bash", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "target rc:" in result.output
    assert "_PLINTH_COMPLETE=bash_source" in result.output
