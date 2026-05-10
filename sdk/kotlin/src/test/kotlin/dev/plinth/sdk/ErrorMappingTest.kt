// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonPrimitive
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull
import kotlin.test.assertTrue

class ErrorMappingTest {
    private lateinit var server: MockWebServer

    @BeforeTest fun setup() {
        server = MockWebServer().apply { start() }
    }

    @AfterTest fun teardown() {
        server.shutdown()
    }

    @Test fun `plinthErrorFromEnvelope - 401 without body maps to Unauthorized`() {
        val err = plinthErrorFromEnvelope(
            statusCode = 401,
            envelope = null,
            retryAfterHeader = null,
        )
        assertTrue(err is PlinthError.Unauthorized)
    }

    @Test fun `plinthErrorFromEnvelope - 404 with fallback code maps to WorkspaceNotFound`() {
        val err = plinthErrorFromEnvelope(
            statusCode = 404,
            envelope = null,
            retryAfterHeader = null,
            fallbackNotFoundCode = PlinthErrorCode.WORKSPACE_NOT_FOUND,
        )
        assertTrue(err is PlinthError.WorkspaceNotFound)
    }

    @Test fun `plinthErrorFromEnvelope - 429 with retry-after header`() {
        val err = plinthErrorFromEnvelope(
            statusCode = 429,
            envelope = null,
            retryAfterHeader = "12",
        )
        assertTrue(err is PlinthError.RateLimited)
        assertEquals(12.0, (err as PlinthError.RateLimited).retryAfterSeconds)
    }

    @Test fun `plinthErrorFromEnvelope - 429 with envelope detail wins over header`() {
        val env = PlinthErrorEnvelope(
            PlinthErrorEnvelope.ErrorPayload(
                code = PlinthErrorCode.RATE_LIMITED,
                message = "slow down",
                details = mapOf("retry_after_seconds" to JsonPrimitive(5.5)),
            )
        )
        val err = plinthErrorFromEnvelope(
            statusCode = 429,
            envelope = env,
            retryAfterHeader = "12",
        )
        assertTrue(err is PlinthError.RateLimited)
        assertEquals(5.5, (err as PlinthError.RateLimited).retryAfterSeconds)
    }

    @Test fun `plinthErrorFromEnvelope - 500 falls through to Server`() {
        val env = PlinthErrorEnvelope(
            PlinthErrorEnvelope.ErrorPayload(
                code = "BACKEND_DOWN",
                message = "kaboom",
            )
        )
        val err = plinthErrorFromEnvelope(
            statusCode = 500,
            envelope = env,
            retryAfterHeader = null,
        )
        assertTrue(err is PlinthError.Server)
        err as PlinthError.Server
        assertEquals(500, err.statusCode)
        assertEquals("BACKEND_DOWN", err.code)
        assertEquals("kaboom", err.message)
    }

    @Test fun `401 end-to-end maps through HTTP client`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(
            MockResponse()
                .setResponseCode(401)
                .setBody("""{"error": {"code": "UNAUTHORIZED", "message": "bad token"}}""")
        )
        val ex = assertFailsWith<PlinthError.Unauthorized> { client.listWorkspaces() }
        assertTrue(ex.message!!.contains("bad token"))
    }

    @Test fun `parseRetryAfter returns null on missing inputs`() {
        assertNull(parseRetryAfter(null, null))
        assertNull(parseRetryAfter("", null))
    }
}
