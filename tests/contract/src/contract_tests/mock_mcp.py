# SPDX-License-Identifier: Apache-2.0
"""Mock-MCP server contract loader.

The mock-mcp server is the reference MCP implementation that ships with
Plinth and is exercised by every example. It does not have a checked-in
OpenAPI document under ``specs/openapi/`` because the tool catalogue is
generated at runtime from the fixtures dir; we still want to enforce that
the routes documented in CONTRACTS.md (`/healthz`, `/tools`, `/invoke`)
are wired up in the running app.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .runner import SpecPaths, openapi_spec_path

SERVICE = "mock-mcp"


def is_importable() -> bool:
    """True if the mock-mcp server package + its runtime deps are available."""
    try:
        from mock_mcp import server  # noqa: F401
        from mock_mcp import settings  # noqa: F401
    except Exception:
        return False
    return True


def build_app() -> Any:
    """Build the mock-mcp FastAPI app with safe defaults for in-process use."""
    if not is_importable():
        raise ImportError("mock_mcp not importable")

    from mock_mcp.server import create_app  # type: ignore[import-not-found]
    from mock_mcp.settings import Settings  # type: ignore[import-not-found]

    fixtures_dir = Path(tempfile.mkdtemp(prefix="plinth-contract-mock-mcp-"))
    os.environ.setdefault("PLINTH_MOCK_FIXTURES_DIR", str(fixtures_dir))
    settings = Settings(fixtures_dir=fixtures_dir)
    return create_app(settings)


def load_actual_spec() -> dict[str, Any]:
    app = build_app()
    return app.openapi()


def load_expected_spec() -> dict[str, Any] | None:
    """Return the on-disk spec, or ``None`` if no spec is checked in.

    The mock-mcp server doesn't currently ship a checked-in OpenAPI document,
    so this returns ``None`` for now. The associated test still asserts the
    healthz / tools / invoke surface from CONTRACTS.md.
    """
    path = openapi_spec_path(SERVICE)
    if not path.exists():
        return None
    import yaml

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


def actual_paths() -> SpecPaths:
    return SpecPaths.from_doc(load_actual_spec())


def expected_paths() -> SpecPaths | None:
    spec = load_expected_spec()
    if spec is None:
        return None
    return SpecPaths.from_doc(spec)
