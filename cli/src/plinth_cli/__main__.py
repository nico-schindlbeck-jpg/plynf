# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Entrypoint for ``python -m plinth_cli``.

Mirrors the ``plinth`` console script defined in ``pyproject.toml``. The
console script points here so the import path is identical regardless of
how the CLI is invoked.
"""

from __future__ import annotations

from .main import cli


def main() -> int:
    """Run the Click app and return its exit code.

    Click never returns from ``cli()`` under the default ``standalone_mode``
    (it raises ``SystemExit``); we wrap it so callers can bypass that for
    tests and so the ``[project.scripts]`` entry has a stable signature.
    """

    cli(prog_name="plinth", standalone_mode=True)  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":  # pragma: no cover - exec'd via ``python -m``
    raise SystemExit(main())
