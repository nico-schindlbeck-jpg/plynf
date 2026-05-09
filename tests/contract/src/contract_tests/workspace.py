# SPDX-License-Identifier: Apache-2.0
"""Workspace service contract loader."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .runner import SpecPaths, load_yaml_spec

SERVICE = "workspace"


def is_importable() -> bool:
    """Return True if the workspace service + its runtime deps are available.

    We import the API module specifically (not just the package root) because
    the latter is dependency-light; the api submodule pulls in
    fastapi / structlog / etc., which is what the contract tests actually need.
    """
    try:
        from plinth_workspace import api  # noqa: F401
        from plinth_workspace import settings  # noqa: F401
    except Exception:
        return False
    return True


def build_app() -> Any:
    """Build the workspace FastAPI app with safe defaults for in-process use.

    Uses a unique temp data dir so multiple test runs don't trip on stale
    SQLite state. Auto-migrate is off because we never persist past process
    exit and migrations have their own dedicated tests.
    """
    if not is_importable():
        raise ImportError("plinth_workspace not importable")

    from plinth_workspace.api import create_app  # type: ignore[import-not-found]
    from plinth_workspace.settings import Settings  # type: ignore[import-not-found]

    data_dir = Path(tempfile.mkdtemp(prefix="plinth-contract-workspace-"))
    os.environ.setdefault("PLINTH_DATA_DIR", str(data_dir))

    settings = Settings(data_dir=data_dir)
    return create_app(settings)


def load_actual_spec() -> dict[str, Any]:
    """Return the live ``app.openapi()`` document."""
    app = build_app()
    return app.openapi()


def load_expected_spec() -> dict[str, Any]:
    """Return the on-disk OpenAPI spec for the workspace service."""
    return load_yaml_spec(SERVICE)


def actual_paths() -> SpecPaths:
    return SpecPaths.from_doc(load_actual_spec())


def expected_paths() -> SpecPaths:
    return SpecPaths.from_doc(load_expected_spec())
