// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class KVTest {
    private lateinit var server: MockWebServer

    @BeforeTest fun setup() {
        server = MockWebServer().apply { start() }
    }

    @AfterTest fun teardown() {
        server.shutdown()
    }

    private suspend fun makeWorkspace(): Workspace {
        val client = mockServerClient(server)
        server.enqueue(MockResponse().setBody(Fixtures.WORKSPACE_LIST_JSON))
        return client.workspace("my-research")
    }

    @Test fun `set returns entry`() = runTest {
        val ws = makeWorkspace()
        server.enqueue(MockResponse().setBody(Fixtures.KV_ENTRY_JSON))
        val entry = ws.kv.set("topic", "renewable energy")
        assertEquals("topic", entry.key)
        assertEquals(1, entry.version)
    }

    @Test fun `set sends value wrapper`() = runTest {
        val ws = makeWorkspace()
        server.enqueue(MockResponse().setBody(Fixtures.KV_ENTRY_JSON))
        ws.kv.set("topic", "renewable energy")
        server.takeRequest() // discard the list call
        val req = server.takeRequest()
        assertEquals("PUT", req.method)
        val body = Json.parseToJsonElement(req.body.readUtf8()) as JsonObject
        assertEquals("renewable energy", body["value"]!!.jsonPrimitive.content)
    }

    @Test fun `get typed string`() = runTest {
        val ws = makeWorkspace()
        server.enqueue(MockResponse().setBody(Fixtures.KV_ENTRY_JSON))
        val value = ws.kv.get<String>("topic")
        assertEquals("renewable energy", value)
    }

    @Test fun `get maps key not found`() = runTest {
        val ws = makeWorkspace()
        server.enqueue(
            MockResponse()
                .setResponseCode(404)
                .setBody("""{"error": {"code": "KEY_NOT_FOUND", "message": "nope"}}""")
        )
        assertFailsWith<PlinthError.KeyNotFound> { ws.kv.get<String>("missing") }
    }

    @Test fun `history returns versions`() = runTest {
        val ws = makeWorkspace()
        server.enqueue(MockResponse().setBody(Fixtures.KV_HISTORY_JSON))
        val versions = ws.kv.history("topic")
        assertEquals(2, versions.size)
        assertEquals(1, versions[0].version)
        assertEquals(2, versions[1].version)
    }

    @Test fun `delete sends DELETE`() = runTest {
        val ws = makeWorkspace()
        server.enqueue(MockResponse().setResponseCode(204))
        ws.kv.delete("topic")
        server.takeRequest() // discard list
        val req = server.takeRequest()
        assertEquals("DELETE", req.method)
    }

    @Test fun `keys with spaces are path-encoded`() = runTest {
        val ws = makeWorkspace()
        server.enqueue(MockResponse().setBody(Fixtures.KV_ENTRY_JSON))
        ws.kv.set("key with space", "x")
        server.takeRequest() // discard list
        val req = server.takeRequest()
        // OkHttp's encoded segment encodes ' ' as %20.
        assertEquals("/v1/workspaces/ws_test_1/kv/key%20with%20space", req.path)
    }
}
