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

class FilesTest {
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

    @Test fun `write text returns metadata`() = runTest {
        val ws = makeWorkspace()
        server.enqueue(MockResponse().setBody(Fixtures.FILE_ENTRY_JSON))
        val meta = ws.files.write("report.md", "# Report\n")
        assertEquals("report.md", meta.path)
        assertEquals(1, meta.version)
    }

    @Test fun `write text sends UTF-8 body`() = runTest {
        val ws = makeWorkspace()
        server.enqueue(MockResponse().setBody(Fixtures.FILE_ENTRY_JSON))
        ws.files.write("report.md", "hello")
        server.takeRequest() // discard list
        val req = server.takeRequest()
        assertEquals("hello", req.body.readUtf8())
        assertEquals("text/plain; charset=utf-8", req.getHeader("Content-Type"))
    }

    @Test fun `read returns bytes`() = runTest {
        val ws = makeWorkspace()
        server.enqueue(MockResponse().setBody("# Report\n"))
        val data = ws.files.read("report.md")
        assertEquals("# Report\n", data.toString(Charsets.UTF_8))
    }

    @Test fun `read text decodes UTF-8`() = runTest {
        val ws = makeWorkspace()
        server.enqueue(MockResponse().setBody("hello"))
        val text = ws.files.readText("report.md")
        assertEquals("hello", text)
    }

    @Test fun `read maps file not found`() = runTest {
        val ws = makeWorkspace()
        server.enqueue(
            MockResponse()
                .setResponseCode(404)
                .setBody("""{"error": {"code": "FILE_NOT_FOUND", "message": "nope"}}""")
        )
        assertFailsWith<PlinthError.FileNotFound> { ws.files.read("missing.md") }
    }

    @Test fun `nested paths are encoded segment-wise`() = runTest {
        val ws = makeWorkspace()
        server.enqueue(MockResponse().setBody(Fixtures.FILE_ENTRY_JSON))
        ws.files.write("dir/sub/file.md", "x")
        server.takeRequest() // discard list
        val req = server.takeRequest()
        assertEquals(
            "/v1/workspaces/ws_test_1/files/dir/sub/file.md",
            req.path
        )
    }
}
