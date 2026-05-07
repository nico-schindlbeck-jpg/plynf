# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workload registry.

Each workload module exposes a :func:`build` function returning a
``WorkloadFn`` (a coroutine that performs ONE request and returns a
``RequestSample``). The ``REGISTRY`` map is the source of truth for the
CLI's workload names.
"""

from __future__ import annotations

from collections.abc import Callable

from . import (
    gateway_invoke_cached,
    gateway_invoke_cold,
    identity_token_issue,
    workspace_files,
    workspace_kv,
    workspace_snapshot,
)

# A factory: given (base_url, headers) returns a workload coroutine.
# Modules must re-export ``build``.
WorkloadFactory = Callable[..., object]

REGISTRY: dict[str, WorkloadModule] = {  # type: ignore[name-defined]
    "workspace_kv": workspace_kv,
    "workspace_files": workspace_files,
    "workspace_snapshot": workspace_snapshot,
    "gateway_invoke_cached": gateway_invoke_cached,
    "gateway_invoke_cold": gateway_invoke_cold,
    "identity_token_issue": identity_token_issue,
}


# Order used by ``plinth-bench all`` — matches CONTRACTS.md docs.
STANDARD_SUITE: list[str] = [
    "workspace_kv",
    "workspace_files",
    "workspace_snapshot",
    "gateway_invoke_cached",
    "gateway_invoke_cold",
    "identity_token_issue",
]


def default_target_url(workload: str) -> str:
    """Map a workload to its sensible default base URL."""

    if workload.startswith("workspace_"):
        return "http://localhost:7421"
    if workload.startswith("gateway_"):
        return "http://localhost:7422"
    if workload.startswith("identity_"):
        return "http://localhost:7425"
    return "http://localhost:7421"


__all__ = [
    "REGISTRY",
    "STANDARD_SUITE",
    "default_target_url",
]
