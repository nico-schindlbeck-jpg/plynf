// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import okhttp3.mockwebserver.MockWebServer

/** Test fixtures (canned JSON bodies) shared by every test file. */
object Fixtures {
    const val WORKSPACE_JSON = """
        {
            "id": "ws_test_1",
            "name": "my-research",
            "created_at": "2026-05-01T10:00:00Z",
            "updated_at": "2026-05-01T10:00:00Z",
            "metadata": {}
        }
    """

    const val WORKSPACE_LIST_JSON = """
        {
            "workspaces": [
                {
                    "id": "ws_test_1",
                    "name": "my-research",
                    "created_at": "2026-05-01T10:00:00Z",
                    "updated_at": "2026-05-01T10:00:00Z",
                    "metadata": {}
                }
            ]
        }
    """

    const val EMPTY_WORKSPACE_LIST_JSON = """{"workspaces": []}"""

    const val KV_ENTRY_JSON = """
        {
            "workspace_id": "ws_test_1",
            "key": "topic",
            "value": "renewable energy",
            "version": 1,
            "created_at": "2026-05-01T10:00:00Z",
            "deleted": false
        }
    """

    const val KV_HISTORY_JSON = """
        {
            "versions": [
                {
                    "workspace_id": "ws_test_1",
                    "key": "topic",
                    "value": "fossil fuels",
                    "version": 1,
                    "created_at": "2026-05-01T09:00:00Z",
                    "deleted": false
                },
                {
                    "workspace_id": "ws_test_1",
                    "key": "topic",
                    "value": "renewable energy",
                    "version": 2,
                    "created_at": "2026-05-01T10:00:00Z",
                    "deleted": false
                }
            ]
        }
    """

    const val FILE_ENTRY_JSON = """
        {
            "workspace_id": "ws_test_1",
            "path": "report.md",
            "size": 32,
            "sha256": "deadbeef",
            "content_type": "text/markdown",
            "version": 1,
            "created_at": "2026-05-01T10:00:00Z",
            "deleted": false
        }
    """

    const val INVOKE_RESPONSE_JSON = """
        {
            "tool_id": "web.fetch",
            "arguments": {"url": "mock://example"},
            "result": {"content": "ok"},
            "cached": false,
            "duration_ms": 12,
            "audit_id": "evt_1",
            "cost_estimate_usd": 0.0
        }
    """

    const val TOKEN_ISSUE_RESPONSE_JSON = """
        {
            "token": "ey.fake.jwt",
            "jti": "jti_1",
            "expires_at": "2026-05-01T11:00:00Z",
            "claims": {
                "sub": "my-agent",
                "iss": "http://localhost:7425",
                "aud": "plinth",
                "iat": 1000,
                "exp": 4600,
                "jti": "jti_1",
                "agent_id": "my-agent",
                "tenant_id": "default",
                "scopes": ["tool:web.fetch:read"]
            }
        }
    """

    const val TOKEN_CLAIMS_JSON = """
        {
            "sub": "my-agent",
            "iss": "http://localhost:7425",
            "aud": "plinth",
            "iat": 1000,
            "exp": 4600,
            "jti": "jti_1",
            "agent_id": "my-agent",
            "tenant_id": "default",
            "scopes": ["tool:web.fetch:read"]
        }
    """
}

/**
 * Build a [Plinth] client wired to the running MockWebServer.
 */
fun mockServerClient(server: MockWebServer, identityServer: MockWebServer? = null): Plinth {
    return Plinth(PlinthConfig(
        workspaceUrl = server.url("/").toString().trimEnd('/'),
        gatewayUrl = server.url("/").toString().trimEnd('/'),
        identityUrl = identityServer?.url("/")?.toString()?.trimEnd('/'),
        apiKey = "local-dev",
    ))
}
