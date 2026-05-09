# SPDX-License-Identifier: Apache-2.0
"""Identity service contract loader.

Identity is checked even when no on-disk OpenAPI spec exists yet; in that
case ``load_expected_spec`` returns ``None`` and the consuming tests skip
spec-comparison checks while still verifying the app's own self-consistency.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from .runner import SpecPaths, openapi_spec_path

SERVICE = "identity"


def is_importable() -> bool:
    """Return True if the identity service + its runtime deps are available."""
    try:
        from plinth_identity import api  # noqa: F401
        from plinth_identity import settings  # noqa: F401
    except Exception:
        return False
    return True


def build_app() -> Any:
    """Build the identity FastAPI app with safe defaults for in-process use."""
    if not is_importable():
        raise ImportError("plinth_identity not importable")

    from plinth_identity.api import create_app  # type: ignore[import-not-found]
    from plinth_identity.settings import Settings  # type: ignore[import-not-found]

    data_dir = Path(tempfile.mkdtemp(prefix="plinth-contract-identity-"))
    os.environ.setdefault("PLINTH_IDENTITY_DATA_DIR", str(data_dir))
    # Provide an in-process JWT secret so the service starts cleanly. We do
    # not persist this; the directory is wiped at the end of the test run.
    secret = secrets.token_hex(32)
    settings = Settings(
        data_dir=data_dir,
        identity_jwt_secret=secret,
        identity_auto_generate_secret=False,
    )
    return create_app(settings)


def load_actual_spec() -> dict[str, Any]:
    app = build_app()
    return app.openapi()


def load_expected_spec() -> dict[str, Any] | None:
    """Return the on-disk spec, or ``None`` if no spec is checked in yet."""
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
