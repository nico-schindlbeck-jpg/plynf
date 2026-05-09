# SPDX-License-Identifier: Apache-2.0
"""Gateway service contract loader."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .runner import SpecPaths, load_yaml_spec

SERVICE = "gateway"


def is_importable() -> bool:
    """Return True if the gateway service + its runtime deps are available."""
    try:
        from plinth_gateway import api  # noqa: F401
        from plinth_gateway import settings  # noqa: F401
    except Exception:
        return False
    return True


def build_app() -> Any:
    """Build the gateway FastAPI app with safe defaults for in-process use."""
    if not is_importable():
        raise ImportError("plinth_gateway not importable")

    from plinth_gateway.api import create_app  # type: ignore[import-not-found]
    from plinth_gateway.settings import Settings  # type: ignore[import-not-found]

    data_dir = Path(tempfile.mkdtemp(prefix="plinth-contract-gateway-"))
    os.environ.setdefault("PLINTH_DATA_DIR", str(data_dir))

    try:
        settings = Settings(data_dir=data_dir)
    except TypeError:
        # Older Settings signatures may not accept data_dir as kwarg.
        settings = Settings()
    return create_app(settings)


def load_actual_spec() -> dict[str, Any]:
    app = build_app()
    return app.openapi()


def load_expected_spec() -> dict[str, Any]:
    return load_yaml_spec(SERVICE)


def actual_paths() -> SpecPaths:
    return SpecPaths.from_doc(load_actual_spec())


def expected_paths() -> SpecPaths:
    return SpecPaths.from_doc(load_expected_spec())
