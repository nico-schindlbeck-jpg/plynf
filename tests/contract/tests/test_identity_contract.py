# SPDX-License-Identifier: Apache-2.0
"""Identity service — contract tests."""

from __future__ import annotations

import pytest

from contract_tests import identity


pytestmark = pytest.mark.skipif(
    not identity.is_importable(),
    reason="plinth_identity not importable on PYTHONPATH",
)


def test_identity_app_exposes_health() -> None:
    """Every Plinth service must expose /healthz."""
    actual = identity.actual_paths()
    assert "/healthz" in actual.paths


def test_identity_v1_paths_present() -> None:
    """Identity always serves at least the /v1 surface."""
    actual = identity.actual_paths()
    v1_paths = [p for p in actual.paths if p.startswith("/v1/")]
    assert v1_paths, "Identity must expose at least one /v1/* path"


def test_identity_optional_spec_matches_when_present() -> None:
    """If a spec is checked in for identity, the running app must satisfy it."""
    expected = identity.expected_paths()
    if expected is None:
        pytest.skip("specs/openapi/identity.yaml not present yet")
    actual = identity.actual_paths()
    missing = expected.paths - actual.paths
    assert not missing, f"Documented paths missing from app: {sorted(missing)}"
