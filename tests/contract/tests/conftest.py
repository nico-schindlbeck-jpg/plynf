# SPDX-License-Identifier: Apache-2.0
"""Test-suite-wide fixtures and helpers.

We register the workspace / gateway / identity service `src/` paths on
``sys.path`` so the suite can import them without a parent ``pip install``.
This keeps the tests runnable in a sparse worktree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from contract_tests.runner import repo_root


def _add_service_to_sys_path(service: str) -> None:
    src = repo_root() / "services" / service / "src"
    if src.exists() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


for svc in ("workspace", "gateway", "identity", "dashboard"):
    _add_service_to_sys_path(svc)


# mock-mcp lives outside services/, at the repo root.
_mock_mcp_src = repo_root() / "mock-mcp-server" / "src"
if _mock_mcp_src.exists() and str(_mock_mcp_src) not in sys.path:
    sys.path.insert(0, str(_mock_mcp_src))


@pytest.fixture(scope="session")
def specs_dir() -> Path:
    return repo_root() / "specs" / "openapi"


@pytest.fixture(scope="session")
def snapshots_dir() -> Path:
    return Path(__file__).parent / "snapshots"
