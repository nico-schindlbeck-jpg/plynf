// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class EncodingTest {
    private val json = HttpClient.defaultJson()

    @Test fun `encodePathSegment escapes slash`() {
        assertEquals("a%2Fb", encodePathSegment("a/b"))
    }

    @Test fun `encodeFilePath preserves slash`() {
        assertEquals("dir/sub/file.md", encodeFilePath("dir/sub/file.md"))
    }

    @Test fun `encodeFilePath escapes spaces`() {
        assertEquals(
            "dir/with%20space/file.md",
            encodeFilePath("dir/with space/file.md"),
        )
    }

    @Test fun `encodeFilePath strips leading slash`() {
        assertEquals("a/b.md", encodeFilePath("/a/b.md"))
    }

    @Test fun `KVEntry decodes from wire format`() {
        val entry = json.decodeFromString(KVEntry.serializer(), Fixtures.KV_ENTRY_JSON.trim())
        assertEquals("ws_test_1", entry.workspaceId)
        assertEquals("topic", entry.key)
        assertEquals(1, entry.version)
    }

    @Test fun `WorkspaceRecord round trip preserves fields`() {
        val raw = Fixtures.WORKSPACE_JSON.trim()
        val record = json.decodeFromString(WorkspaceRecord.serializer(), raw)
        assertEquals("ws_test_1", record.id)
        assertEquals("my-research", record.name)

        val reencoded = json.encodeToString(WorkspaceRecord.serializer(), record)
        assertTrue(reencoded.contains("\"id\":\"ws_test_1\""))
        assertTrue(reencoded.contains("\"name\":\"my-research\""))
        // snake_case wire format preserved.
        assertTrue(reencoded.contains("\"created_at\""))
        assertTrue(reencoded.contains("\"updated_at\""))
    }

    @Test fun `error envelope decodes`() {
        val raw = """{"error": {"code": "WORKSPACE_NOT_FOUND", "message": "no"}}"""
        val env = json.decodeFromString(PlinthErrorEnvelope.serializer(), raw)
        assertEquals("WORKSPACE_NOT_FOUND", env.error.code)
        assertEquals("no", env.error.message)
    }
}
