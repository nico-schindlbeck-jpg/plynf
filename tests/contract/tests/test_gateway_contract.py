# SPDX-License-Identifier: Apache-2.0
"""Gateway service — contract tests."""

from __future__ import annotations

import pytest

from contract_tests import gateway


pytestmark = pytest.mark.skipif(
    not gateway.is_importable(),
    reason="plinth_gateway not importable on PYTHONPATH",
)


def test_gateway_documented_paths_exist_in_app() -> None:
    expected = gateway.expected_paths()
    actual = gateway.actual_paths()
    missing = expected.paths - actual.paths
    assert not missing, f"Documented paths missing from app: {sorted(missing)}"


def test_gateway_documented_methods_exist_in_app() -> None:
    expected = gateway.expected_paths()
    actual = gateway.actual_paths()
    missing = expected.methods - actual.methods
    assert not missing, f"Documented (path, method) pairs missing: {sorted(missing)}"


def test_gateway_invoke_endpoints_are_present() -> None:
    actual = gateway.actual_paths()
    assert "/v1/invoke" in actual.paths
    assert ("/v1/invoke", "post") in actual.methods


def test_gateway_tools_register_endpoint_present() -> None:
    actual = gateway.actual_paths()
    assert "/v1/tools/register" in actual.paths


def test_gateway_audit_endpoints_present() -> None:
    actual = gateway.actual_paths()
    assert "/v1/audit" in actual.paths
