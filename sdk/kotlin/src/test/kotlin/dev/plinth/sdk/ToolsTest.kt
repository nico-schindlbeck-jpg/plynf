// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class ToolsTest {
    private lateinit var server: MockWebServer

    @BeforeTest fun setup() {
        server = MockWebServer().apply { start() }
    }

    @AfterTest fun teardown() {
        server.shutdown()
    }

    @Test fun `invoke returns result`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(MockResponse().setBody(Fixtures.INVOKE_RESPONSE_JSON))
        val result = client.tools.invoke(
            "web.fetch",
            mapOf("url" to JsonPrimitive("mock://example")),
        )
        assertEquals("web.fetch", result.toolId)
        assertEquals(false, result.cached)
        assertEquals("evt_1", result.auditId)
    }

    @Test fun `invoke posts arguments in body`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(MockResponse().setBody(Fixtures.INVOKE_RESPONSE_JSON))
        client.tools.invoke(
            "web.fetch",
            mapOf("url" to JsonPrimitive("mock://example")),
            Tools.Options(workspaceId = "ws_test_1", agentId = "my-agent"),
        )
        val req = server.takeRequest()
        val body = Json.parseToJsonElement(req.body.readUtf8()) as JsonObject
        assertEquals("web.fetch", body["tool_id"]!!.jsonPrimitive.content)
        assertEquals("ws_test_1", body["workspace_id"]!!.jsonPrimitive.content)
        assertEquals("my-agent", body["agent_id"]!!.jsonPrimitive.content)
        assertEquals(
            "mock://example",
            body["arguments"]!!.jsonObject["url"]!!.jsonPrimitive.content,
        )
    }

    @Test fun `invoke maps tool not found`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(
            MockResponse()
                .setResponseCode(404)
                .setBody("""{"error": {"code": "TOOL_NOT_FOUND", "message": "unknown"}}""")
        )
        assertFailsWith<PlinthError.ToolNotFound> {
            client.tools.invoke("missing", emptyMap())
        }
    }

    @Test fun `invoke maps rate limited with retry-after`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(
            MockResponse()
                .setResponseCode(429)
                .setHeader("Retry-After", "12")
                .setBody("""{"error": {"code": "RATE_LIMITED", "message": "slow down", "details": {"retry_after_seconds": 7.5}}}""")
        )
        val ex = assertFailsWith<PlinthError.RateLimited> {
            client.tools.invoke("web.fetch", emptyMap())
        }
        assertEquals(7.5, ex.retryAfterSeconds)
    }

    @Test fun `list tools decodes array`() = runTest {
        val client = mockServerClient(server)
        val listJson = """
            {
                "tools": [
                    {
                        "tool_id": "web.fetch",
                        "name": "Web Fetch",
                        "description": "Fetch a URL",
                        "transport": "http",
                        "endpoint": "http://mock/tools/web.fetch",
                        "input_schema": {},
                        "output_schema": {},
                        "created_at": "2026-05-01T10:00:00Z",
                        "updated_at": "2026-05-01T10:00:00Z"
                    }
                ]
            }
        """
        server.enqueue(MockResponse().setBody(listJson))
        val tools = client.tools.list()
        assertEquals(1, tools.size)
        assertEquals("web.fetch", tools[0].toolId)
    }

    @Test fun `invoke with Any map converts arguments`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(MockResponse().setBody(Fixtures.INVOKE_RESPONSE_JSON))
        client.tools.invoke(
            "web.fetch",
            mapOf("url" to "mock://x", "count" to 5),
        )
        val req = server.takeRequest()
        val body = Json.parseToJsonElement(req.body.readUtf8()) as JsonObject
        val args = body["arguments"]!!.jsonObject
        assertEquals("mock://x", args["url"]!!.jsonPrimitive.content)
        assertEquals(5, args["count"]!!.jsonPrimitive.content.toInt())
    }
}
