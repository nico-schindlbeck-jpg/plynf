# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared pytest fixtures for the CLI test-suite.

Each test gets:

* a tmp config directory (no real ``~/.plinth`` is touched)
* clean ``PLINTH_*`` environment variables
* a :class:`click.testing.CliRunner` instance
* a forced ``--output=json`` mode so assertions stay deterministic
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from plinth_cli.main import cli

# ---------------------------------------------------------------------------
# Environment isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Scrub ``PLINTH_*`` env vars and stub HOME to a temp directory."""

    for var in list(os.environ):
        if var.startswith("PLINTH_"):
            monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    yield


@pytest.fixture
def runner() -> CliRunner:
    """A Click CLI runner with mix_stderr=False so we can inspect both streams."""

    return CliRunner(mix_stderr=False)


@pytest.fixture
def app():  # noqa: ANN201 - returns the click group
    """The root :class:`click.Group` under test."""

    return cli


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """Path to a config file the test can write/read freely."""

    return tmp_path / "config.toml"


@pytest.fixture
def write_config(config_path: Path):  # noqa: ANN201 - factory closure
    """Return a callable that writes ``content`` to the test config file."""

    def _write(content: str) -> Path:
        config_path.write_text(content)
        return config_path

    return _write


@pytest.fixture
def base_config(write_config) -> Path:  # noqa: ANN001 - fixture
    """A minimal default config pointing every URL at the local-dev ports."""

    write_config(
        """
[default]
workspace_url = "http://workspace.test"
gateway_url   = "http://gateway.test"
identity_url  = "http://identity.test"
api_key       = "test-api-key"
output        = "human"

[profiles.production]
workspace_url = "http://prod-workspace.test"
gateway_url   = "http://prod-gateway.test"
identity_url  = "http://prod-identity.test"
api_key_env   = "PLINTH_PROD_API_KEY"
output        = "json"
"""
    )
    return Path(os.environ["HOME"]) / ".plinth" / "config.toml"  # placeholder for monkeypatch


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def parse_json_output(text: str) -> Any:
    """Find the first JSON document in ``text`` and parse it."""

    text = text.strip()
    return json.loads(text)


@pytest.fixture
def parse_json():  # noqa: ANN201 - return helper
    return parse_json_output
