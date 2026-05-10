# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Asana MCP-style server for Plinth.

Wraps a small slice of the Asana REST API (workspaces, projects, tasks).
The server reads the user's bearer token from the inbound
``Authorization: Bearer ...`` header (the gateway forwards the OAuth token
end-to-end). Asana's API base is ``https://app.asana.com/api/1.0``.
"""

__version__ = "1.5.0"
