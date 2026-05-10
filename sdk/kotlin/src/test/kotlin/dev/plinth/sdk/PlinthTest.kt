// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

class PlinthInitTest {

    private lateinit var server: MockWebServer

    @BeforeTest fun setup() {
        server = MockWebServer().apply { start() }
    }

    @AfterTest fun teardown() {
        server.shutdown()
    }

    @Test fun `init requires api key`() {
        val ex = assertFailsWith<PlinthError.InvalidConfig> {
            Plinth(PlinthConfig(apiKey = ""))
        }
        assertTrue(ex.message!!.contains("apiKey", ignoreCase = true))
    }

    @Test fun `init rejects malformed workspace url`() {
        assertFailsWith<PlinthError.InvalidConfig> {
            Plinth(PlinthConfig(workspaceUrl = "not a url", apiKey = "x"))
        }
    }

    @Test fun `init wires identity when url present`() {
        val client = Plinth(PlinthConfig(
            workspaceUrl = server.url("/").toString().trimEnd('/'),
            gatewayUrl = server.url("/").toString().trimEnd('/'),
            identityUrl = server.url("/").toString().trimEnd('/'),
            apiKey = "x",
        ))
        assertTrue(client.hasIdentity)
        assertNotNull(client.identity)
    }

    @Test fun `identity accessor throws when not configured`() {
        val client = Plinth(PlinthConfig(
            workspaceUrl = server.url("/").toString().trimEnd('/'),
            gatewayUrl = server.url("/").toString().trimEnd('/'),
            apiKey = "x",
        ))
        assertFailsWith<PlinthError.IdentityNotConfigured> { client.identity }
    }

    @Test fun `requests carry bearer auth header`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(MockResponse().setBody(Fixtures.WORKSPACE_LIST_JSON))
        client.listWorkspaces()
        val req = server.takeRequest()
        assertEquals("Bearer local-dev", req.getHeader("Authorization"))
    }

    @Test fun `requests carry user agent header`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(MockResponse().setBody(Fixtures.WORKSPACE_LIST_JSON))
        client.listWorkspaces()
        val req = server.takeRequest()
        assertEquals("plinth-sdk-kotlin/0.1.0", req.getHeader("User-Agent"))
    }
}
