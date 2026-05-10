// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation

/// A per-workspace handle. Bundles the cached server record plus typed
/// sub-clients (``kv``, ``files``).
///
/// Reusable across tasks/actors since the underlying ``HTTPClient`` is
/// `Sendable` and `URLSession` is thread-safe.
public struct Workspace: Sendable {
    /// Snapshot of the server-side workspace at the time this handle
    /// was constructed. Re-fetch via ``Plinth/getWorkspace(id:)`` if
    /// you need fresher metadata.
    public let record: WorkspaceRecord

    /// Versioned key-value store for this workspace.
    public var kv: KV {
        KV(http: http, workspaceID: record.id)
    }

    /// Versioned file store for this workspace.
    public var files: Files {
        Files(http: http, workspaceID: record.id)
    }

    /// Stable workspace ID (`ws_…`).
    public var id: String { record.id }

    /// Human-readable workspace name.
    public var name: String { record.name }

    let http: HTTPClient

    init(record: WorkspaceRecord, http: HTTPClient) {
        self.record = record
        self.http = http
    }
}

/// Wire-level workspace record. The richer ``Workspace`` handle wraps
/// this and exposes the typed sub-clients (``Workspace/kv``,
/// ``Workspace/files``).
public struct WorkspaceRecord: Codable, Sendable, Equatable {
    public let id: String
    public let name: String
    public let createdAt: Date
    public let updatedAt: Date
    public let metadata: [String: AnyCodableValue]?

    public init(
        id: String,
        name: String,
        createdAt: Date,
        updatedAt: Date,
        metadata: [String: AnyCodableValue]? = nil
    ) {
        self.id = id
        self.name = name
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.metadata = metadata
    }
}

struct WorkspaceListResponseInternal: Decodable, Sendable {
    let workspaces: [WorkspaceRecord]
}
