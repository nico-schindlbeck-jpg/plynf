# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for ``plinth_workspace.settings``."""

from __future__ import annotations

from pathlib import Path

import pytest

from plinth_workspace.settings import Settings, get_settings


def test_defaults() -> None:
    s = Settings()
    assert s.data_dir == Path("/tmp/plinth-data")
    assert s.workspace_port == 7421
    assert s.workspace_host == "0.0.0.0"
    assert s.log_level == "INFO"
    assert s.log_format == "console"
    assert s.auth_required is False
    assert s.db_path == Path("/tmp/plinth-data/workspace.db")
    assert s.blobs_dir == Path("/tmp/plinth-data/blobs")


def test_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PLINTH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PLINTH_WORKSPACE_PORT", "9999")
    monkeypatch.setenv("PLINTH_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("PLINTH_LOG_FORMAT", "json")
    monkeypatch.setenv("PLINTH_AUTH_REQUIRED", "true")

    s = get_settings()
    assert s.data_dir == tmp_path
    assert s.workspace_port == 9999
    assert s.log_level == "DEBUG"
    assert s.log_format == "json"
    assert s.auth_required is True


def test_explicit_overrides_beat_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PLINTH_WORKSPACE_PORT", "1111")
    s = get_settings(data_dir=tmp_path, workspace_port=2222)
    assert s.workspace_port == 2222
    assert s.data_dir == tmp_path


def test_invalid_log_format(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("PLINTH_LOG_FORMAT", "yaml")
    with pytest.raises(ValidationError):
        get_settings()
