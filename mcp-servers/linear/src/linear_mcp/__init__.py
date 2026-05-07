# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Linear MCP-style server for Plinth.

Exposes a small set of tools that wrap Linear's GraphQL API. The server reads
the access token from the inbound ``Authorization: Bearer ...`` header (the
gateway forwards the user's OAuth token end-to-end).
"""

__version__ = "0.4.0"
