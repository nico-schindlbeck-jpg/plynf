# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Coverage for ``plinth_workspace.__main__``.

We don't actually want uvicorn to bind a port during tests, so we monkeypatch
``uvicorn.run`` to record its call.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_main_invokes_uvicorn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PLINTH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PLINTH_WORKSPACE_PORT", "29999")
    monkeypatch.setenv("PLINTH_WORKSPACE_HOST", "127.0.0.1")
    monkeypatch.setenv("PLINTH_LOG_LEVEL", "WARNING")

    captured: dict[str, object] = {}

    def fake_run(app, **kwargs) -> None:  # noqa: ANN001
        captured["app"] = app
        captured.update(kwargs)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)

    from plinth_workspace.__main__ import main

    main()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 29999
    assert captured["app"] is not None
