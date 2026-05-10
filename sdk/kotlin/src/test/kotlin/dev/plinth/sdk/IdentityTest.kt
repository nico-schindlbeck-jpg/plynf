// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class IdentityTest {
    private lateinit var server: MockWebServer

    @BeforeTest fun setup() {
        server = MockWebServer().apply { start() }
    }

    @AfterTest fun teardown() {
        server.shutdown()
    }

    private fun client(): Plinth {
        return Plinth(PlinthConfig(
            workspaceUrl = server.url("/").toString().trimEnd('/'),
            gatewayUrl = server.url("/").toString().trimEnd('/'),
            identityUrl = server.url("/").toString().trimEnd('/'),
            apiKey = "local-dev",
        ))
    }

    @Test fun `issue token returns response`() = runTest {
        val c = client()
        server.enqueue(MockResponse().setResponseCode(201).setBody(Fixtures.TOKEN_ISSUE_RESPONSE_JSON))
        val response = c.identity.issueToken(
            agentId = "my-agent",
            scopes = listOf("tool:web.fetch:read"),
            ttlSeconds = 3600,
        )
        assertEquals("jti_1", response.jti)
        assertEquals("my-agent", response.claims.agentId)
    }

    @Test fun `issue token sends scopes and agent`() = runTest {
        val c = client()
        server.enqueue(MockResponse().setResponseCode(201).setBody(Fixtures.TOKEN_ISSUE_RESPONSE_JSON))
        c.identity.issueToken(
            agentId = "my-agent",
            scopes = listOf("tool:web.fetch:read"),
            ttlSeconds = 3600,
        )
        val req = server.takeRequest()
        val body = Json.parseToJsonElement(req.body.readUtf8()) as JsonObject
        assertEquals("my-agent", body["agent_id"]!!.jsonPrimitive.content)
        assertEquals(3600, body["ttl_seconds"]!!.jsonPrimitive.content.toInt())
        assertEquals(
            "tool:web.fetch:read",
            body["scopes"]!!.jsonArray[0].jsonPrimitive.content,
        )
    }

    @Test fun `verify token returns claims`() = runTest {
        val c = client()
        server.enqueue(MockResponse().setBody(Fixtures.TOKEN_CLAIMS_JSON))
        val claims = c.identity.verifyToken("ey.fake.jwt")
        assertEquals("my-agent", claims.agentId)
        assertEquals(listOf("tool:web.fetch:read"), claims.scopes)
    }

    @Test fun `verify token maps unauthorized`() = runTest {
        val c = client()
        server.enqueue(
            MockResponse()
                .setResponseCode(401)
                .setBody("""{"error": {"code": "TOKEN_EXPIRED", "message": "expired"}}""")
        )
        assertFailsWith<PlinthError.Unauthorized> { c.identity.verifyToken("expired") }
    }

    @Test fun `revoke token sends DELETE`() = runTest {
        val c = client()
        server.enqueue(MockResponse().setResponseCode(204))
        c.identity.revokeToken("jti_1")
        val req = server.takeRequest()
        assertEquals("DELETE", req.method)
        assertEquals("/v1/tokens/jti_1/revoke", req.path)
    }
}
