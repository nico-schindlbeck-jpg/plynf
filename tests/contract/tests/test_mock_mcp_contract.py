# SPDX-License-Identifier: Apache-2.0
"""Mock-MCP server — contract tests.

The mock-mcp server doesn't ship a checked-in OpenAPI document yet, so the
checks here pin the small subset of routes that are part of the v1
contract per CONTRACTS.md (`/healthz`, `/tools`, `/invoke`). When a spec
is added under ``specs/openapi/mock-mcp.yaml`` the existence-checks below
upgrade automatically (the same way ``test_identity_contract.py`` does).
"""

from __future__ import annotations

import pytest

from contract_tests import mock_mcp


pytestmark = pytest.mark.skipif(
    not mock_mcp.is_importable(),
    reason="mock_mcp not importable on PYTHONPATH",
)


def test_mock_mcp_app_exposes_health() -> None:
    """Every Plinth service must expose /healthz."""
    actual = mock_mcp.actual_paths()
    assert "/healthz" in actual.paths


def test_mock_mcp_tools_endpoint_present() -> None:
    """The /tools listing endpoint is part of the v1 surface."""
    actual = mock_mcp.actual_paths()
    assert "/tools" in actual.paths
    assert ("/tools", "get") in actual.methods


def test_mock_mcp_invoke_endpoint_present() -> None:
    """The /invoke/{tool_name} endpoint is part of the v1 surface."""
    actual = mock_mcp.actual_paths()
    invoke_paths = [p for p in actual.paths if p.startswith("/invoke")]
    assert invoke_paths, "mock-mcp must expose at least one /invoke path"
    # Every invoke route is POST.
    invoke_methods = {(p, m) for (p, m) in actual.methods if p.startswith("/invoke")}
    assert any(m == "post" for (_, m) in invoke_methods), "/invoke must accept POST"


def test_mock_mcp_optional_spec_matches_when_present() -> None:
    """If a spec is checked in for mock-mcp, the running app must satisfy it."""
    expected = mock_mcp.expected_paths()
    if expected is None:
        pytest.skip("specs/openapi/mock-mcp.yaml not present yet")
    actual = mock_mcp.actual_paths()
    missing = expected.paths - actual.paths
    assert not missing, f"Documented paths missing from app: {sorted(missing)}"


def test_mock_mcp_extra_paths_are_documented_or_meta() -> None:
    """Extra routes must be a known prefix or FastAPI meta."""
    actual = mock_mcp.actual_paths()
    allowed_meta = {"/healthz", "/metrics", "/openapi.json", "/docs", "/redoc"}
    for path in actual.paths:
        if path in allowed_meta:
            continue
        assert path.startswith(("/tools", "/invoke", "/v1/")), (
            f"Undocumented mock-mcp path: {path}"
        )
