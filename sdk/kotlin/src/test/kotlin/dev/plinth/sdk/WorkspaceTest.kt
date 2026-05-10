// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class WorkspaceTest {
    private lateinit var server: MockWebServer

    @BeforeTest fun setup() {
        server = MockWebServer().apply { start() }
    }

    @AfterTest fun teardown() {
        server.shutdown()
    }

    @Test fun `workspace returns existing when found`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(MockResponse().setBody(Fixtures.WORKSPACE_LIST_JSON))
        val ws = client.workspace("my-research")
        assertEquals("ws_test_1", ws.id)
        assertEquals("my-research", ws.name)
        // Only the GET request was issued — no POST.
        assertEquals(1, server.requestCount)
    }

    @Test fun `workspace creates when not found`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(MockResponse().setBody(Fixtures.EMPTY_WORKSPACE_LIST_JSON))
        server.enqueue(MockResponse().setBody(Fixtures.WORKSPACE_JSON).setResponseCode(201))
        val ws = client.workspace("my-research")
        assertEquals("ws_test_1", ws.id)
        assertEquals(2, server.requestCount)
        server.takeRequest() // discard list
        val createRequest = server.takeRequest()
        assertEquals("POST", createRequest.method)
        val body = Json.parseToJsonElement(createRequest.body.readUtf8()) as JsonObject
        assertEquals("my-research", body["name"].toString().trim('"'))
    }

    @Test fun `list workspaces parses array`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(MockResponse().setBody(Fixtures.WORKSPACE_LIST_JSON))
        val workspaces = client.listWorkspaces()
        assertEquals(1, workspaces.size)
        assertEquals("ws_test_1", workspaces[0].id)
    }

    @Test fun `get workspace fetches by id`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(MockResponse().setBody(Fixtures.WORKSPACE_JSON))
        val ws = client.getWorkspace("ws_test_1")
        assertEquals("ws_test_1", ws.id)
        val req = server.takeRequest()
        assertEquals("/v1/workspaces/ws_test_1", req.path)
    }

    @Test fun `get workspace maps not found`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(
            MockResponse()
                .setResponseCode(404)
                .setBody("""{"error": {"code": "WORKSPACE_NOT_FOUND", "message": "nope"}}""")
        )
        assertFailsWith<PlinthError.WorkspaceNotFound> {
            client.getWorkspace("ws_missing")
        }
    }

    @Test fun `delete workspace issues DELETE`() = runTest {
        val client = mockServerClient(server)
        server.enqueue(MockResponse().setResponseCode(204))
        client.deleteWorkspace("ws_test_1")
        val req = server.takeRequest()
        assertEquals("DELETE", req.method)
        assertEquals("/v1/workspaces/ws_test_1", req.path)
    }

    @Test fun `workspace get-or-create tiebreak on latest updated`() = runTest {
        val client = mockServerClient(server)
        val listJson = """
            {
                "workspaces": [
                    {
                        "id": "ws_old",
                        "name": "dup",
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "metadata": {}
                    },
                    {
                        "id": "ws_new",
                        "name": "dup",
                        "created_at": "2026-05-01T00:00:00Z",
                        "updated_at": "2026-05-01T00:00:00Z",
                        "metadata": {}
                    }
                ]
            }
        """
        server.enqueue(MockResponse().setBody(listJson))
        val ws = client.workspace("dup")
        assertEquals("ws_new", ws.id)
    }
}
