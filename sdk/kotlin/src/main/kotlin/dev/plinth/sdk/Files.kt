// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

/**
 * Versioned file (blob) store for a workspace.
 *
 * Every [write] creates a new immutable version. Reads default to the
 * latest version.
 *
 * Construct via [Workspace.files].
 */
class Files internal constructor(
    private val http: HttpClient,
    private val workspaceId: String,
) {

    /**
     * Upload raw bytes to [path]. Returns the resulting versioned
     * metadata.
     */
    suspend fun write(
        path: String,
        bytes: ByteArray,
        contentType: String = "application/octet-stream",
    ): FileEntry = http.putRaw(
        path = filePath(path),
        body = bytes,
        contentType = contentType,
        responseSerializer = FileEntry.serializer(),
        notFoundCode = PlinthErrorCode.WORKSPACE_NOT_FOUND,
    )

    /**
     * Upload UTF-8 text to [path]. Sets `Content-Type` to
     * `text/plain; charset=utf-8` by default.
     */
    suspend fun write(
        path: String,
        text: String,
        contentType: String = "text/plain; charset=utf-8",
    ): FileEntry = write(path, text.toByteArray(Charsets.UTF_8), contentType)

    /** Read the raw bytes at [path] (latest version). */
    suspend fun read(path: String): ByteArray =
        http.getBytes(
            path = filePath(path),
            notFoundCode = PlinthErrorCode.FILE_NOT_FOUND,
        )

    /** Read a specific historical [version] of [path]. */
    suspend fun readVersion(path: String, version: Int): ByteArray =
        http.getBytes(
            path = filePath(path),
            query = mapOf("version" to version.toString()),
            notFoundCode = PlinthErrorCode.FILE_NOT_FOUND,
        )

    /** Read [path] decoded as UTF-8 text. */
    suspend fun readText(path: String): String =
        read(path).toString(Charsets.UTF_8)

    /** Metadata for [path] (without downloading bytes). */
    suspend fun meta(path: String): FileEntry =
        http.getJson(
            path = "${filePath(path)}/meta",
            deserializer = FileEntry.serializer(),
            notFoundCode = PlinthErrorCode.FILE_NOT_FOUND,
        )

    /** Tombstone the file at [path]. */
    suspend fun delete(path: String) {
        http.delete(filePath(path), notFoundCode = PlinthErrorCode.FILE_NOT_FOUND)
    }

    /** Metadata for every file in the workspace. */
    suspend fun list(): List<FileEntry> =
        http.getJson(
            path = "/v1/workspaces/${encodePathSegment(workspaceId)}/files",
            deserializer = FilesListResponse.serializer(),
            notFoundCode = PlinthErrorCode.WORKSPACE_NOT_FOUND,
        ).files

    // MARK: - Helpers

    private fun filePath(p: String): String =
        "/v1/workspaces/${encodePathSegment(workspaceId)}/files/${encodeFilePath(p)}"
}
