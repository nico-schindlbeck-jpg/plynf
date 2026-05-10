// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation

/// Versioned key-value store for a workspace.
///
/// Every ``set(key:value:)`` creates a new immutable version. Reads
/// default to the latest version; pass an explicit ``version`` to read
/// a specific historical revision.
///
/// Construct via ``Workspace/kv``.
public struct KV: Sendable {
    let http: HTTPClient
    let workspaceID: String

    init(http: HTTPClient, workspaceID: String) {
        self.http = http
        self.workspaceID = workspaceID
    }

    /// Write `value` to `key` and return the resulting versioned entry.
    ///
    /// `value` is JSON-encoded — pass any `Encodable` (including
    /// ``AnyCodableValue``) or a Foundation primitive.
    @discardableResult
    public func set<V: Encodable>(key: String, value: V) async throws -> KVEntry {
        let body = SetRequest(value: value)
        return try await http.putJSON(
            path(for: key),
            body: body,
            notFoundCode: PlinthErrorCode.workspaceNotFound
        )
    }

    /// Convenience: set an arbitrary Foundation value (Bool, Int, Double,
    /// String, Array, Dictionary).
    @discardableResult
    public func setAny(key: String, value: Any?) async throws -> KVEntry {
        return try await set(key: key, value: AnyCodableValue.from(value))
    }

    /// Read the latest value for `key`, decoded as `V`.
    ///
    /// Throws ``PlinthError/keyNotFound`` when the key was deleted or
    /// never written.
    public func get<V: Decodable>(key: String, as type: V.Type = V.self) async throws -> V {
        let entry: KVEntry = try await getEntry(key: key)
        // The wire value is wrapped in AnyCodableValue; re-encode then
        // decode into the user's target type for a transparent cast.
        let raw = try http.encoder.encode(entry.value)
        do {
            return try http.decoder.decode(V.self, from: raw)
        } catch {
            throw PlinthError.decoding(error.localizedDescription)
        }
    }

    /// Read the latest entry (value + metadata) for `key`.
    public func getEntry(key: String) async throws -> KVEntry {
        return try await http.getJSON(
            path(for: key),
            notFoundCode: PlinthErrorCode.keyNotFound
        )
    }

    /// Read a specific historical version of `key`.
    public func getVersion(key: String, version: Int) async throws -> KVEntry {
        return try await http.getJSON(
            path(for: key),
            query: ["version": String(version)],
            notFoundCode: PlinthErrorCode.keyNotFound
        )
    }

    /// Every recorded version of `key`, oldest first.
    public func history(key: String) async throws -> [KVEntry] {
        let resp: KVHistoryResponse = try await http.getJSON(
            "\(path(for: key))/history",
            notFoundCode: PlinthErrorCode.keyNotFound
        )
        return resp.versions
    }

    /// Tombstone `key`. Reads after this throw ``PlinthError/keyNotFound``.
    public func delete(key: String) async throws {
        try await http.delete(
            path(for: key),
            notFoundCode: PlinthErrorCode.keyNotFound
        )
    }

    /// Latest entry for every key in the workspace.
    public func list() async throws -> [KVEntry] {
        let resp: KVListResponse = try await http.getJSON(
            "/v1/workspaces/\(encodePathSegment(workspaceID))/kv",
            notFoundCode: PlinthErrorCode.workspaceNotFound
        )
        return resp.entries
    }

    // MARK: - Internal helpers

    func path(for key: String) -> String {
        return "/v1/workspaces/\(encodePathSegment(workspaceID))/kv/\(encodePathSegment(key))"
    }

    /// Request body wrapper for PUT /v1/workspaces/{id}/kv/{key}.
    struct SetRequest<V: Encodable>: Encodable {
        let value: V
    }
}
