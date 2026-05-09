# SPDX-License-Identifier: Apache-2.0
"""Helpers shared by the per-service contract tests.

These intentionally avoid bringing in heavyweight OpenAPI tooling (prance,
openapi-core, etc.). The frozen v1 surface is small enough that a few
hand-written set / dict diffs cover the contract obligations precisely:

- "every documented path exists" → set difference on path keys.
- "every documented method exists" → set difference on (path, method) pairs.
- "every documented response code exists" → set difference on
  (path, method, status) triples.
- "no breaking changes" → snapshot diff with a small breaking-change
  classifier.

Schema-level checks were considered and rejected: a spec frozen at v1.0.0
will be tweaked for documentation reasons (descriptions, examples) far more
often than its semantic shape, and those edits should not require tests
turning red.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Repo root is three parents up from this file when laid out as
# tests/contract/src/contract_tests/runner.py — but allow override for CI.
_THIS_FILE = Path(__file__).resolve()
DEFAULT_REPO_ROOT = _THIS_FILE.parents[4]


def repo_root() -> Path:
    """Return the Plinth repo root.

    Honours ``PLINTH_REPO_ROOT`` so the suite can be relocated. Falls back
    to the directory three levels above this file.
    """
    override = os.environ.get("PLINTH_REPO_ROOT")
    if override:
        return Path(override).resolve()
    return DEFAULT_REPO_ROOT


def openapi_spec_path(service: str) -> Path:
    """Path to the on-disk OpenAPI spec for ``service``."""
    return repo_root() / "specs" / "openapi" / f"{service}.yaml"


def load_yaml_spec(service: str) -> dict[str, Any]:
    """Load and return the OpenAPI spec YAML for ``service`` as a dict."""
    path = openapi_spec_path(service)
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


@dataclass
class SpecPaths:
    """Flat path view of an OpenAPI document.

    Captures just enough structure to do contract diffs without needing a
    real OpenAPI library. ``status_codes`` maps ``(path, method) -> set of
    response status codes`` (string keys, since OpenAPI uses ``"200"``).
    """

    paths: set[str] = field(default_factory=set)
    methods: set[tuple[str, str]] = field(default_factory=set)
    status_codes: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    operation_ids: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "SpecPaths":
        """Build a :class:`SpecPaths` from a parsed OpenAPI document."""
        out = cls()
        for path, ops in (doc.get("paths") or {}).items():
            if not isinstance(ops, dict):
                continue
            out.paths.add(path)
            for method, op in ops.items():
                method_l = method.lower()
                if method_l not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                    continue
                if not isinstance(op, dict):
                    continue
                key = (path, method_l)
                out.methods.add(key)
                op_id = op.get("operationId")
                if isinstance(op_id, str):
                    out.operation_ids[key] = op_id
                statuses = set()
                responses = op.get("responses") or {}
                for code in responses.keys():
                    statuses.add(str(code))
                out.status_codes[key] = statuses
        return out


@dataclass
class BreakingChange:
    """A single breaking change between two specs."""

    kind: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.detail}"


def diff_specs(old: SpecPaths, new: SpecPaths) -> list[BreakingChange]:
    """Return a list of breaking changes when going ``old`` → ``new``.

    Breaking = removed path, removed method on a kept path, removed status
    code on a kept (path, method). Additions are not breaking.
    """
    out: list[BreakingChange] = []
    removed_paths = old.paths - new.paths
    for p in sorted(removed_paths):
        out.append(BreakingChange("removed-path", p))

    kept_paths = old.paths & new.paths
    for path in sorted(kept_paths):
        old_methods = {m for (p, m) in old.methods if p == path}
        new_methods = {m for (p, m) in new.methods if p == path}
        for m in sorted(old_methods - new_methods):
            out.append(BreakingChange("removed-method", f"{m.upper()} {path}"))

    common_methods = old.methods & new.methods
    for key in sorted(common_methods):
        old_statuses = old.status_codes.get(key, set())
        new_statuses = new.status_codes.get(key, set())
        for status in sorted(old_statuses - new_statuses):
            out.append(
                BreakingChange(
                    "removed-status",
                    f"{key[1].upper()} {key[0]} {status}",
                )
            )

    return out
