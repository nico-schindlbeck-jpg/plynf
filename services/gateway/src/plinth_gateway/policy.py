# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Capability-token policy. v0.1 stub: always allow.

A future ``Identity`` service will issue capability tokens with the shape
``{agent_id, workspace_id, tool_scopes, expires_at}``. The gateway will then
verify the token here before invoking. For now this is a pure stub so the
call site is wired up.
"""

from __future__ import annotations

from typing import Any


def check_capability(
    *,
    tool_id: str,
    workspace_id: str | None,
    agent_id: str | None,
    token: str | None,
    capabilities: dict[str, Any] | None = None,
) -> bool:
    """Check whether the caller may invoke ``tool_id``.

    Always returns ``True`` in v0.1. Signature is shaped for the v0.2
    capability flow.
    """
    _ = (tool_id, workspace_id, agent_id, token, capabilities)
    return True
