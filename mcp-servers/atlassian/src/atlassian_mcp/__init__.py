# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Atlassian (Jira + Confluence) MCP-style server for Plinth.

Exposes a small set of tools that wrap the Atlassian REST APIs accessible
via the OAuth 2.0 (3LO) ``cloudid``-prefixed routes:

* Jira REST v3: ``/ex/jira/{cloudid}/rest/api/3/...``
* Confluence v2: ``/ex/confluence/{cloudid}/wiki/api/v2/...``

The server reads the access token from the inbound ``Authorization: Bearer
...`` header and the workspace's cloudid from the
``X-Plinth-OAuth-Cloudid`` header (the gateway populates both from the
encrypted OAuth connection on every invoke).
"""

__version__ = "1.5.0"
