# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Unified ``plinth`` CLI.

A single Python command consolidating service ops, workflow control, audit
queries, tenant management, and benchmark orchestration. See CONTRACTS.md
("Unified CLI: ``plinth``") for the full surface.

The CLI is a thin wrapper over the Plinth Python SDK with config-driven
endpoints, profile switching, and rich/JSON output. It is distributed as
the standalone ``plinth-cli`` package and ships an entry point named
``plinth``.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
