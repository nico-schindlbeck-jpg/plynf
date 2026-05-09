# SPDX-License-Identifier: Apache-2.0
"""Breaking-change detection.

Diffs the current OpenAPI spec at ``specs/openapi/<service>.yaml`` against
the snapshot at ``tests/contract/tests/snapshots/<service>.yaml`` and
flags removed paths / methods / status codes (which would constitute a
breaking change under the v1 stability promise).

The snapshot is committed alongside the test suite. To deliberately break
the v1 surface (which only happens on a v2 boundary), update the snapshot
in the same commit and document the migration in ``docs/API_STABILITY.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from contract_tests.runner import (
    SpecPaths,
    diff_specs,
    load_yaml_spec,
    repo_root,
)


def _load_snapshot(service: str) -> dict[str, Any] | None:
    snapshot = Path(__file__).parent / "snapshots" / f"{service}.yaml"
    if not snapshot.exists():
        return None
    with snapshot.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{snapshot} did not parse to a mapping")
    return data


@pytest.mark.parametrize("service", ["workspace", "gateway"])
def test_no_breaking_changes_against_snapshot(service: str) -> None:
    """The current spec must not break compatibility with the snapshot."""
    snap = _load_snapshot(service)
    if snap is None:
        pytest.skip(f"snapshots/{service}.yaml not present")
    current = load_yaml_spec(service)

    old = SpecPaths.from_doc(snap)
    new = SpecPaths.from_doc(current)

    diffs = diff_specs(old, new)
    assert not diffs, "Breaking changes detected vs. snapshot:\n" + "\n".join(str(d) for d in diffs)


def test_snapshot_files_exist() -> None:
    """Both snapshot files must be checked in."""
    snap_dir = Path(__file__).parent / "snapshots"
    assert (snap_dir / "workspace.yaml").exists()
    assert (snap_dir / "gateway.yaml").exists()


def test_snapshot_paths_are_strict_subset_of_current() -> None:
    """Every snapshot path must still resolve in the current spec.

    This is the crisp v1-stability invariant: snapshot ⊆ current.
    """
    for service in ("workspace", "gateway"):
        snap = _load_snapshot(service)
        if snap is None:
            continue
        current = load_yaml_spec(service)

        old = SpecPaths.from_doc(snap)
        new = SpecPaths.from_doc(current)

        removed = old.paths - new.paths
        assert not removed, f"{service}: paths removed since snapshot: {sorted(removed)}"


def test_repo_root_resolves() -> None:
    """The runner must locate the Plinth repo root unambiguously."""
    root = repo_root()
    assert root.is_dir()
    assert (root / "specs" / "openapi").is_dir()
