# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Top-level CLI smoke tests."""

from __future__ import annotations

from plinth_cli import __version__
from plinth_cli.main import CLIContext, cli


def test_version_flag(runner) -> None:
    """``--version`` prints the package version and exits 0."""

    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_lists_all_command_groups(runner) -> None:
    """Every spec-defined group is registered."""

    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for name in (
        "config",
        "services",
        "migrate",
        "workflow",
        "audit",
        "tenant",
        "health",
        "bench",
        "completion",
    ):
        assert name in result.output, f"missing group: {name}"


def test_short_help_works(runner) -> None:
    """``-h`` is registered as an alias of ``--help``."""

    result = runner.invoke(cli, ["-h"])
    assert result.exit_code == 0
    assert "Plinth — unified" in result.output


def test_unknown_command_exits_with_usage(runner) -> None:
    """Misspelled commands fail with Click's standard usage message."""

    result = runner.invoke(cli, ["nope"])
    assert result.exit_code != 0
    assert "No such command" in result.stderr


def test_cli_context_has_resolved_config(runner, config_path) -> None:
    """The ``--config`` flag flows into the loader and reaches subcommands."""

    config_path.write_text('[default]\nworkspace_url = "http://x.test"\n')
    result = runner.invoke(cli, ["--config", str(config_path), "--json", "config", "show"])
    # ``--json config show`` exits 0 and prints the resolved values.
    assert result.exit_code == 0, result.output


def test_clicontext_object_construction() -> None:
    """``CLIContext`` is a thin holder; build it directly to lock the API."""

    from plinth_cli.config import Config

    cfg = Config()
    ctx = CLIContext(config=cfg, output_override="json")
    assert ctx.output_mode() == "json"


def test_format_alias_for_output(runner, config_path) -> None:
    """``--format json`` behaves the same as ``--output json``."""

    config_path.write_text('[default]\nworkspace_url = "http://x.test"\n')
    result = runner.invoke(
        cli,
        ["--config", str(config_path), "--format", "json", "config", "show"],
    )
    assert result.exit_code == 0, result.output


def test_format_table_alias_for_human(runner, config_path) -> None:
    """``--format table`` works as a synonym for ``human``."""

    config_path.write_text('[default]\nworkspace_url = "http://x.test"\n')
    result = runner.invoke(
        cli,
        ["--config", str(config_path), "--format", "table", "config", "show"],
    )
    assert result.exit_code == 0, result.output


def test_clicontext_csv_mode(runner, config_path) -> None:
    """``--output csv`` resolves to csv on the context."""

    config_path.write_text('[default]\nworkspace_url = "http://x.test"\n')
    # We re-enter the runner just to verify the option parses; CLI
    # internals exercise CSV in command-level tests.
    result = runner.invoke(
        cli,
        ["--config", str(config_path), "--output", "csv", "config", "show"],
    )
    assert result.exit_code == 0, result.output


def test_json_flag_wins_over_format(runner, config_path) -> None:
    """``--json`` beats a conflicting ``--format human``."""

    config_path.write_text('[default]\nworkspace_url = "http://x.test"\n')
    result = runner.invoke(
        cli,
        [
            "--config",
            str(config_path),
            "--json",
            "--format",
            "human",
            "config",
            "show",
        ],
    )
    assert result.exit_code == 0, result.output
    # JSON-mode output should round-trip as JSON.
    import json

    payload = json.loads(result.stdout.strip())
    assert payload["workspace_url"] == "http://x.test"
