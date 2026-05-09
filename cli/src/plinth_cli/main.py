# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Top-level Click app for the ``plinth`` CLI.

This module wires the ``--profile`` / ``--output`` / ``--config`` global
options, loads :class:`plinth_cli.config.Config` once per invocation, and
attaches all command groups (services, migrate, workflow, audit, tenant,
health, bench, completion, config).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from . import __version__
from .config import ConfigError, load_config
from .output import emit_error

# ---------------------------------------------------------------------------
# Top-level command
# ---------------------------------------------------------------------------


# A single Click context object carries the resolved config + raw output
# override down to every subcommand. Subcommands read it via
# ``click.pass_context`` -> ``ctx.obj``.


@click.group(
    name="plinth",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Plinth — unified ops + admin CLI.\n\n"
        "Manage services, run migrations, inspect workflows, query audit "
        "events, administer tenants, and run benchmarks from one place."
    ),
)
@click.version_option(__version__, "-V", "--version", prog_name="plinth")
@click.option(
    "--profile",
    default="default",
    metavar="NAME",
    help="Config profile to use (default: 'default').",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override the config file path (default: ~/.plinth/config.toml).",
)
@click.option(
    "--output",
    type=click.Choice(
        ["human", "table", "json", "csv"],
        case_sensitive=False,
    ),
    default=None,
    help="Output format. Defaults to 'human' on TTY, 'json' when piped.",
)
@click.option(
    "--format",
    "fmt_alias",
    type=click.Choice(
        ["human", "table", "json", "csv"],
        case_sensitive=False,
    ),
    default=None,
    help="Alias for --output. 'table' is a synonym for 'human'.",
)
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    default=False,
    help="Shortcut for --output=json.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    profile: str,
    config_path: Path | None,
    output: str | None,
    fmt_alias: str | None,
    json_flag: bool,
) -> None:
    """Entry point. Loads config and stashes it in ``ctx.obj``."""

    try:
        cfg = load_config(profile=profile, config_path=config_path)
    except ConfigError as exc:
        emit_error(str(exc))
        ctx.exit(2)
        return

    # Resolution order: ``--json`` > ``--output`` > ``--format``. Users
    # never pass two of these intentionally; the priority just keeps the
    # behaviour predictable.
    raw_output: str | None
    if json_flag:
        raw_output = "json"
    elif output is not None:
        raw_output = output
    else:
        raw_output = fmt_alias
    ctx.obj = CLIContext(config=cfg, output_override=raw_output)


# ---------------------------------------------------------------------------
# Shared context object
# ---------------------------------------------------------------------------


class CLIContext:
    """Per-invocation context shared by every subcommand.

    Carries the resolved :class:`Config`, the raw ``--output`` override (or
    None), and a lazy :class:`Plinth` SDK client built on demand. Holding
    the SDK client here means commands don't open HTTP connections until
    they actually need to.
    """

    def __init__(self, config: Any, output_override: str | None) -> None:
        self.config = config
        self.output_override = output_override
        self._sdk_client: Any = None

    def output_mode(self) -> str:
        """Resolve the effective output mode (human vs json)."""

        from .output import resolve_mode

        return resolve_mode(self.output_override, self.config.output)

    def sdk(self) -> Any:
        """Return a singleton :class:`plinth.Plinth` instance.

        Imports lazily so ``plinth --help`` still works without the SDK
        installed (rare, but useful when shipping the CLI standalone).
        """

        if self._sdk_client is None:
            from plinth import Plinth

            self._sdk_client = Plinth(
                workspace_url=self.config.workspace_url,
                gateway_url=self.config.gateway_url,
                identity_url=self.config.identity_url,
                api_key=self.config.api_key or "local-dev",
                timeout=self.config.timeout,
            )
        return self._sdk_client


# ---------------------------------------------------------------------------
# Attach command groups
# ---------------------------------------------------------------------------


def _attach_subcommands() -> None:
    """Register every command group on the root ``cli`` group.

    Done in a helper so import order is explicit + the function can be
    unit-tested in isolation. Each command module exposes a ``group``
    attribute (a ``click.Group``).
    """

    from .commands import (
        audit,
        bench,
        completion,
        health,
        migrate,
        services,
        tenant,
        workflow,
    )
    from .commands import (
        config as config_cmd,
    )

    cli.add_command(config_cmd.group, name="config")
    cli.add_command(services.group, name="services")
    cli.add_command(migrate.group, name="migrate")
    cli.add_command(workflow.group, name="workflow")
    cli.add_command(audit.group, name="audit")
    cli.add_command(tenant.group, name="tenant")
    cli.add_command(health.group, name="health")
    cli.add_command(bench.group, name="bench")
    cli.add_command(completion.group, name="completion")


_attach_subcommands()


__all__ = ["CLIContext", "cli"]
