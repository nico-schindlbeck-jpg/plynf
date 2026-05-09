# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for :mod:`plinth_cli.config` + the ``plinth config`` command group."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plinth_cli.config import (
    Config,
    ConfigError,
    load_config,
    write_default_config,
)
from plinth_cli.main import cli

# ---------------------------------------------------------------------------
# Loader unit tests
# ---------------------------------------------------------------------------


def test_load_config_with_no_file_uses_defaults(tmp_path: Path) -> None:
    """Missing config file → built-in defaults stand in."""

    cfg = load_config(config_path=tmp_path / "missing.toml", env={})
    assert isinstance(cfg, Config)
    assert cfg.workspace_url == "http://localhost:7421"
    assert cfg.gateway_url == "http://localhost:7422"
    assert cfg.identity_url == "http://localhost:7425"
    assert cfg.api_key == "local-dev"
    assert cfg.config_path is None


def test_load_config_reads_default_section(tmp_path: Path) -> None:
    """``[default]`` keys override the built-in defaults."""

    p = tmp_path / "c.toml"
    p.write_text(
        """
[default]
workspace_url = "http://w.example"
gateway_url   = "http://g.example"
api_key       = "abc"
"""
    )
    cfg = load_config(config_path=p, env={})
    assert cfg.workspace_url == "http://w.example"
    assert cfg.gateway_url == "http://g.example"
    assert cfg.api_key == "abc"
    assert cfg.config_path == p


def test_load_config_profile_switching(tmp_path: Path) -> None:
    """Selecting a profile layers its keys over ``[default]``."""

    p = tmp_path / "c.toml"
    p.write_text(
        """
[default]
workspace_url = "http://default.test"

[profiles.prod]
workspace_url = "http://prod.test"
gateway_url   = "http://gw.prod.test"
"""
    )
    cfg = load_config(profile="prod", config_path=p, env={})
    assert cfg.workspace_url == "http://prod.test"
    assert cfg.gateway_url == "http://gw.prod.test"
    assert cfg.profile == "prod"


def test_load_config_unknown_profile_raises(tmp_path: Path) -> None:
    """Picking a profile that doesn't exist surfaces a friendly error."""

    p = tmp_path / "c.toml"
    p.write_text("[default]\n")
    with pytest.raises(ConfigError, match="Profile 'nope'"):
        load_config(profile="nope", config_path=p, env={})


def test_load_config_api_key_env_indirection(tmp_path: Path) -> None:
    """``api_key_env`` resolves through the environment."""

    p = tmp_path / "c.toml"
    p.write_text(
        """
[default]
workspace_url = "http://w.test"
api_key_env   = "MY_KEY"
"""
    )
    cfg = load_config(config_path=p, env={"MY_KEY": "secret"})
    assert cfg.api_key == "secret"


def test_load_config_env_overrides_file(tmp_path: Path) -> None:
    """``PLINTH_*`` env vars beat anything in the file."""

    p = tmp_path / "c.toml"
    p.write_text('[default]\nworkspace_url = "http://from-file.test"\n')
    cfg = load_config(
        config_path=p,
        env={"PLINTH_WORKSPACE_URL": "http://from-env.test"},
    )
    assert cfg.workspace_url == "http://from-env.test"


def test_load_config_invalid_toml(tmp_path: Path) -> None:
    """Bad TOML surfaces as :class:`ConfigError` (not a stack trace)."""

    p = tmp_path / "c.toml"
    p.write_text("this is not [[ valid TOML")
    with pytest.raises(ConfigError):
        load_config(config_path=p, env={})


def test_config_redacts_api_key() -> None:
    """``Config.as_dict`` redacts everything but the last 4 chars."""

    cfg = Config(api_key="abcdef1234")
    rendered = cfg.as_dict()
    assert rendered["api_key"].endswith("1234")
    assert "abcdef" not in rendered["api_key"]


def test_write_default_config_creates_file(tmp_path: Path) -> None:
    """``write_default_config`` writes a non-empty TOML scaffold."""

    target = tmp_path / "nested" / "config.toml"
    written = write_default_config(target)
    assert written == target
    text = target.read_text()
    assert "[default]" in text
    assert "workspace_url" in text


# ---------------------------------------------------------------------------
# CLI command tests
# ---------------------------------------------------------------------------


def test_cli_config_init_writes_file(runner, tmp_path: Path) -> None:
    """``plinth config init --non-interactive`` writes a starter file."""

    target = tmp_path / "config.toml"
    result = runner.invoke(
        cli,
        ["config", "init", "--path", str(target), "--non-interactive"],
    )
    assert result.exit_code == 0, result.output
    assert target.exists()
    assert "[default]" in target.read_text()


def test_cli_config_init_refuses_overwrite_without_force(
    runner, tmp_path: Path
) -> None:
    """Existing files are protected unless ``--force`` is passed."""

    target = tmp_path / "config.toml"
    target.write_text('[default]\nworkspace_url = "http://x"\n')
    result = runner.invoke(
        cli,
        ["config", "init", "--path", str(target), "--non-interactive"],
    )
    assert result.exit_code != 0


def test_cli_config_init_force_overwrites(runner, tmp_path: Path) -> None:
    """``--force`` lets the user overwrite an existing config."""

    target = tmp_path / "config.toml"
    target.write_text("# old\n")
    result = runner.invoke(
        cli,
        ["config", "init", "--path", str(target), "--non-interactive", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert "[default]" in target.read_text()


def test_cli_config_show_prints_resolved_values(runner, tmp_path: Path) -> None:
    """``config show`` redacts the API key and exits 0."""

    target = tmp_path / "config.toml"
    target.write_text(
        '[default]\nworkspace_url = "http://shown.test"\napi_key = "verysecret"\n'
    )
    result = runner.invoke(
        cli,
        ["--config", str(target), "--json", "config", "show"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert payload["workspace_url"] == "http://shown.test"
    assert payload["api_key"].endswith("cret")
    assert "verys" not in payload["api_key"]


def test_cli_profile_flag(runner, tmp_path: Path) -> None:
    """``--profile`` selects an alternate config block."""

    target = tmp_path / "config.toml"
    target.write_text(
        """
[default]
workspace_url = "http://default.test"

[profiles.staging]
workspace_url = "http://staging.test"
"""
    )
    result = runner.invoke(
        cli,
        [
            "--config",
            str(target),
            "--profile",
            "staging",
            "--json",
            "config",
            "show",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip())
    assert payload["profile"] == "staging"
    assert payload["workspace_url"] == "http://staging.test"
