// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation

/// Versioned file (blob) store for a workspace.
///
/// Every ``write(path:data:contentType:)`` creates a new immutable
/// version. Reads default to the latest version.
///
/// Construct via ``Workspace/files``.
public struct Files: Sendable {
    let http: HTTPClient
    let workspaceID: String

    init(http: HTTPClient, workspaceID: String) {
        self.http = http
        self.workspaceID = workspaceID
    }

    /// Upload raw bytes to `path`. Returns the resulting versioned
    /// metadata.
    @discardableResult
    public func write(
        path: String,
        data: Data,
        contentType: String = "application/octet-stream"
    ) async throws -> FileEntry {
        return try await http.putRaw(
            self.path(for: path),
            body: data,
            contentType: contentType,
            notFoundCode: PlinthErrorCode.workspaceNotFound
        )
    }

    /// Upload UTF-8 text to `path`. Sets `Content-Type` to
    /// `text/plain; charset=utf-8` by default.
    @discardableResult
    public func write(
        path: String,
        text: String,
        contentType: String = "text/plain; charset=utf-8"
    ) async throws -> FileEntry {
        let data = Data(text.utf8)
        return try await write(path: path, data: data, contentType: contentType)
    }

    /// Read the raw bytes at `path` (latest version).
    public func read(path: String) async throws -> Data {
        return try await http.getData(
            self.path(for: path),
            notFoundCode: PlinthErrorCode.fileNotFound
        )
    }

    /// Read a specific historical version of `path`.
    public func readVersion(path: String, version: Int) async throws -> Data {
        return try await http.getData(
            self.path(for: path),
            query: ["version": String(version)],
            notFoundCode: PlinthErrorCode.fileNotFound
        )
    }

    /// Read `path` decoded as UTF-8 text.
    public func readText(path: String) async throws -> String {
        let data = try await read(path: path)
        guard let text = String(data: data, encoding: .utf8) else {
            throw PlinthError.decoding("File at \(path) is not valid UTF-8")
        }
        return text
    }

    /// Metadata for `path` (without downloading bytes).
    public func meta(path: String) async throws -> FileEntry {
        return try await http.getJSON(
            "\(self.path(for: path))/meta",
            notFoundCode: PlinthErrorCode.fileNotFound
        )
    }

    /// Tombstone the file at `path`.
    public func delete(path: String) async throws {
        try await http.delete(
            self.path(for: path),
            notFoundCode: PlinthErrorCode.fileNotFound
        )
    }

    /// Metadata for every file in the workspace.
    public func list() async throws -> [FileEntry] {
        let resp: FilesListResponse = try await http.getJSON(
            "/v1/workspaces/\(encodePathSegment(workspaceID))/files",
            notFoundCode: PlinthErrorCode.workspaceNotFound
        )
        return resp.files
    }

    // MARK: - Internal helpers

    func path(for p: String) -> String {
        return "/v1/workspaces/\(encodePathSegment(workspaceID))/files/\(encodeFilePath(p))"
    }
}
