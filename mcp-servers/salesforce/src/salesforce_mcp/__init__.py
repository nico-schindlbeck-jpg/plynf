# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Salesforce MCP-style server for Plinth.

Wraps a small slice of the Salesforce REST API (SOQL queries, object CRUD,
schema describe). Salesforce's OAuth flow returns a per-org ``instance_url``
in the token response — the Plinth gateway captures this and forwards it
verbatim on every proxied invoke as ``X-Plinth-OAuth-InstanceUrl``. The MCP
server reads it off the inbound request and uses it as the per-call API base.
"""

__version__ = "1.5.0"
