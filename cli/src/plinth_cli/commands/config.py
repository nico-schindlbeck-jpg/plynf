# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""``plinth config`` — read/write ``~/.plinth/config.toml``.

The CLI itself loads config once at startup; this group only handles the
file lifecycle (interactive init + a no-side-effect ``show``).
"""

from __future__ import annotations

from pathlib import Path

import click

from .. import settings as _s
from ..config import write_default_config
from ..main import CLIContext
from ..output import emit_human, emit_json, emit_kv


@click.group(help="Read/write the Plinth CLI config (~/.plinth/config.toml).")
def group() -> None:
    """Container for the ``config`` subcommands."""


@group.command("init", help="Create ~/.plinth/config.toml interactively.")
@click.option(
    "--workspace-url",
    default=_s.DEFAULT_WORKSPACE_URL,
    show_default=True,
    help="Workspace service base URL.",
)
@click.option(
    "--gateway-url",
    default=_s.DEFAULT_GATEWAY_URL,
    show_default=True,
    help="Gateway service base URL.",
)
@click.option(
    "--identity-url",
    default=_s.DEFAULT_IDENTITY_URL,
    show_default=True,
    help="Identity service base URL.",
)
@click.option(
    "--api-key",
    default=_s.DEFAULT_API_KEY,
    show_default=True,
    help="Bearer token used for both services.",
)
@click.option(
    "--output",
    "output_mode",
    type=click.Choice(["human", "json"], case_sensitive=False),
    default=_s.DEFAULT_OUTPUT,
    show_default=True,
    help="Default output format.",
)
@click.option(
    "--path",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override the config file location (default: ~/.plinth/config.toml).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing config file without prompting.",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    default=False,
    help="Skip prompts; use the option values as-is. Useful for scripts.",
)
def init(
    workspace_url: str,
    gateway_url: str,
    identity_url: str,
    api_key: str,
    output_mode: str,
    config_path: Path | None,
    force: bool,
    non_interactive: bool,
) -> None:
    """Walk the user through a starter config (or write one straight away)."""

    target = config_path if config_path is not None else _s.CONFIG_PATH

    if target.exists() and not force:
        if non_interactive:
            raise click.ClickException(
                f"{target} already exists; pass --force to overwrite."
            )
        if not click.confirm(
            f"{target} already exists. Overwrite?",
            default=False,
        ):
            click.echo("Aborted.")
            return

    if not non_interactive:
        workspace_url = click.prompt("Workspace URL", default=workspace_url)
        gateway_url = click.prompt("Gateway URL", default=gateway_url)
        identity_url = click.prompt("Identity URL", default=identity_url)
        api_key = click.prompt("API key (any non-empty string in dev)", default=api_key)
        output_mode = click.prompt(
            "Default output ('human' or 'json')",
            default=output_mode,
            type=click.Choice(["human", "json"], case_sensitive=False),
        )

    written = write_default_config(
        target,
        workspace_url=workspace_url,
        gateway_url=gateway_url,
        identity_url=identity_url,
        api_key=api_key,
        output=output_mode,
    )
    emit_human(f"[green]wrote[/] {written}")


@group.command("show", help="Print the effective resolved configuration.")
@click.pass_context
def show(ctx: click.Context) -> None:
    """Render the resolved config (with the API key redacted)."""

    cli_ctx: CLIContext = ctx.obj
    cfg = cli_ctx.config

    if cli_ctx.output_mode() == "json":
        emit_json(cfg.as_dict())
        return

    emit_kv(f"plinth config — profile={cfg.profile}", cfg.as_dict())
