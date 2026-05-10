// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement

/**
 * Versioned key-value store for a workspace.
 *
 * Every [set] creates a new immutable version. Reads default to the
 * latest version; pass an explicit version to [getVersion] for a
 * specific historical revision.
 *
 * Construct via [Workspace.kv].
 */
class KV internal constructor(
    @PublishedApi internal val http: HttpClient,
    private val workspaceId: String,
) {
    @PublishedApi internal val json: Json get() = http.json

    /**
     * Write [value] to [key] and return the versioned entry.
     *
     * `value` can be any JSON-serializable Kotlin type — strings, ints,
     * lists, maps, or your own `@Serializable` data classes. Pass a
     * [JsonElement] directly when you've already built one.
     */
    suspend inline fun <reified T> set(key: String, value: T): KVEntry =
        setSerialized(key, json.encodeToJsonElement(value))

    /**
     * Write a pre-built [JsonElement] to [key] without going through
     * the inline reified path. Useful when the serializer is computed
     * at runtime.
     */
    suspend fun setSerialized(key: String, value: JsonElement): KVEntry {
        return http.putJson(
            path = path(key),
            body = KVSetRequest(value),
            bodySerializer = KVSetRequest.serializer(),
            responseSerializer = KVEntry.serializer(),
            notFoundCode = PlinthErrorCode.WORKSPACE_NOT_FOUND,
        )
    }

    /**
     * Read the latest value for [key] decoded as [T].
     *
     * Throws [PlinthError.KeyNotFound] when the key was deleted or
     * never written.
     */
    suspend inline fun <reified T> get(key: String): T {
        val entry = getEntry(key)
        return json.decodeFromJsonElement<T>(entry.value)
    }

    /**
     * Read the latest entry (value + metadata) for [key].
     */
    suspend fun getEntry(key: String): KVEntry =
        http.getJson(
            path = path(key),
            deserializer = KVEntry.serializer(),
            notFoundCode = PlinthErrorCode.KEY_NOT_FOUND,
        )

    /**
     * Read a specific historical [version] of [key].
     */
    suspend fun getVersion(key: String, version: Int): KVEntry =
        http.getJson(
            path = path(key),
            deserializer = KVEntry.serializer(),
            query = mapOf("version" to version.toString()),
            notFoundCode = PlinthErrorCode.KEY_NOT_FOUND,
        )

    /** Every recorded version of [key], oldest first. */
    suspend fun history(key: String): List<KVEntry> =
        http.getJson(
            path = "${path(key)}/history",
            deserializer = KVHistoryResponse.serializer(),
            notFoundCode = PlinthErrorCode.KEY_NOT_FOUND,
        ).versions

    /** Tombstone [key]. Reads after this throw [PlinthError.KeyNotFound]. */
    suspend fun delete(key: String) {
        http.delete(path(key), notFoundCode = PlinthErrorCode.KEY_NOT_FOUND)
    }

    /** Latest entry for every key in the workspace. */
    suspend fun list(): List<KVEntry> =
        http.getJson(
            path = "/v1/workspaces/${encodePathSegment(workspaceId)}/kv",
            deserializer = KVListResponse.serializer(),
            notFoundCode = PlinthErrorCode.WORKSPACE_NOT_FOUND,
        ).entries

    // MARK: - Helpers

    private fun path(key: String): String =
        "/v1/workspaces/${encodePathSegment(workspaceId)}/kv/${encodePathSegment(key)}"
}
