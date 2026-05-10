# SPDX-License-Identifier: Apache-2.0
"""Workspace service — contract tests."""

from __future__ import annotations

import pytest

from contract_tests import workspace


pytestmark = pytest.mark.skipif(
    not workspace.is_importable(),
    reason="plinth_workspace not importable on PYTHONPATH",
)


def test_workspace_documented_paths_exist_in_app() -> None:
    """Every path in the OpenAPI spec exists in the running service."""
    expected = workspace.expected_paths()
    actual = workspace.actual_paths()
    missing = expected.paths - actual.paths
    assert not missing, f"Documented paths missing from app: {sorted(missing)}"


def test_workspace_documented_methods_exist_in_app() -> None:
    """Every (path, method) in the spec is wired in the app."""
    expected = workspace.expected_paths()
    actual = workspace.actual_paths()
    missing_methods = expected.methods - actual.methods
    assert not missing_methods, (
        f"Documented (path, method) pairs missing from app: {sorted(missing_methods)}"
    )


def test_workspace_documented_status_codes_exist_in_app() -> None:
    """Every documented response status code exists in the app's OpenAPI.

    Caveat: 401 and 500 are emitted by FastAPI middleware / exception handlers,
    not by individual route decorators, so they don't appear in app.openapi()
    even though the spec correctly documents them. We treat those two codes as
    middleware-emitted and skip them in the divergence check.
    """
    middleware_emitted = {"401", "500"}
    expected = workspace.expected_paths()
    actual = workspace.actual_paths()
    diffs: list[str] = []
    for key, statuses in expected.status_codes.items():
        actual_statuses = actual.status_codes.get(key, set())
        missing_for_key = (statuses - actual_statuses) - middleware_emitted
        if missing_for_key:
            diffs.append(f"{key[1].upper()} {key[0]} missing statuses: {sorted(missing_for_key)}")
    assert not diffs, "\n".join(diffs)


def test_workspace_v1_paths_have_documented_health() -> None:
    """The /healthz probe must always exist."""
    actual = workspace.actual_paths()
    assert "/healthz" in actual.paths


def test_workspace_extra_paths_are_v1_only() -> None:
    """Any new (extra) path the app exposes must live under /v1/ or /healthz."""
    expected = workspace.expected_paths()
    actual = workspace.actual_paths()
    extras = actual.paths - expected.paths
    bad = [p for p in extras if not (p.startswith("/v1/") or p in {"/healthz", "/metrics", "/openapi.json", "/docs", "/redoc"})]
    assert not bad, f"Undocumented non-v1 paths: {bad}"


def test_workspace_no_v1_path_was_silently_renamed() -> None:
    """Sanity-check a couple of stable v1 paths haven't been renamed."""
    actual = workspace.actual_paths()
    assert "/v1/workspaces" in actual.paths
    assert ("/v1/workspaces", "post") in actual.methods
    assert ("/v1/workspaces", "get") in actual.methods


def test_workspace_kv_endpoints_present() -> None:
    """The KV endpoints documented in v1 must be present."""
    actual = workspace.actual_paths()
    assert "/v1/workspaces/{ws_id}/kv/{key}" in actual.paths
    assert ("/v1/workspaces/{ws_id}/kv/{key}", "get") in actual.methods
    assert ("/v1/workspaces/{ws_id}/kv/{key}", "put") in actual.methods
