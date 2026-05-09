# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Static defaults shared across the CLI.

Anything that varies per deployment lives in :mod:`plinth_cli.config`. This
module is just constants — service ports, default URLs, log + pid dirs —
so unit tests don't have to mock a Settings object just to know where the
workspace runs.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Default URLs (mirror the Makefile)
# ---------------------------------------------------------------------------

DEFAULT_WORKSPACE_URL = "http://localhost:7421"
DEFAULT_GATEWAY_URL = "http://localhost:7422"
DEFAULT_MOCK_MCP_URL = "http://localhost:7423"
DEFAULT_DASHBOARD_URL = "http://localhost:7424"
DEFAULT_IDENTITY_URL = "http://localhost:7425"
DEFAULT_GITHUB_MCP_URL = "http://localhost:7426"
DEFAULT_SLACK_MCP_URL = "http://localhost:7427"
DEFAULT_LINEAR_MCP_URL = "http://localhost:7428"

DEFAULT_API_KEY = "local-dev"
DEFAULT_OUTPUT = "human"  # "human" | "json"
DEFAULT_PROFILE = "default"

# ---------------------------------------------------------------------------
# Filesystem locations (mirror the Makefile / scripts)
# ---------------------------------------------------------------------------

LOG_DIR = Path("/tmp/plinth-logs")
PID_DIR = Path("/tmp/plinth-pids")
DEFAULT_DATA_DIR = Path("/tmp/plinth-data")

CONFIG_HOME = Path.home() / ".plinth"
CONFIG_PATH = CONFIG_HOME / "config.toml"

# ---------------------------------------------------------------------------
# Service registry (used by ``plinth services`` and ``plinth health``).
# Each tuple is (name, default_url, package_module, env_var_for_port).
# ---------------------------------------------------------------------------

SERVICES = [
    ("workspace", DEFAULT_WORKSPACE_URL, "plinth_workspace", "PLINTH_WORKSPACE_PORT"),
    ("gateway", DEFAULT_GATEWAY_URL, "plinth_gateway", "PLINTH_GATEWAY_PORT"),
    ("identity", DEFAULT_IDENTITY_URL, "plinth_identity", "PLINTH_IDENTITY_PORT"),
    ("dashboard", DEFAULT_DASHBOARD_URL, "plinth_dashboard", "PLINTH_DASHBOARD_PORT"),
    ("mock-mcp", DEFAULT_MOCK_MCP_URL, "mock_mcp", "PLINTH_MOCK_MCP_PORT"),
    ("github-mcp", DEFAULT_GITHUB_MCP_URL, "github_mcp", "PLINTH_GITHUB_MCP_PORT"),
    ("slack-mcp", DEFAULT_SLACK_MCP_URL, "slack_mcp", "PLINTH_SLACK_MCP_PORT"),
    ("linear-mcp", DEFAULT_LINEAR_MCP_URL, "linear_mcp", "PLINTH_LINEAR_MCP_PORT"),
]


def service_names() -> list[str]:
    """Return the canonical list of managed service names."""

    return [name for name, *_ in SERVICES]
