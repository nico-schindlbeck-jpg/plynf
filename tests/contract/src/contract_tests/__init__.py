# SPDX-License-Identifier: Apache-2.0
"""Plinth contract test suite.

This package is loaded by the pytest tests under ``tests/`` and provides:

- :mod:`contract_tests.runner` — small helpers shared by every per-service
  contract test (loading OpenAPI specs, computing path / method diffs).
- :mod:`contract_tests.workspace` / ``gateway`` / ``identity`` — per-service
  wiring that builds an in-process FastAPI app and exposes the live
  OpenAPI document for diffing against the on-disk spec.

The suite is intentionally tolerant: when a service package isn't importable
on the current Python path (e.g. running the tests in a CI image that hasn't
installed every service), the check skips rather than fails. This keeps the
suite usable as a precommit hook in partial worktrees.
"""

from __future__ import annotations

__all__ = [
    "runner",
    "workspace",
    "gateway",
    "identity",
    "mock_mcp",
]

__version__ = "1.0.0"
