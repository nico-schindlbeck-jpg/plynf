// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation

/// Gateway tool surface: invoke, list, register, deregister.
///
/// Reachable via ``Plinth/tools``.
public struct Tools: Sendable {
    let http: HTTPClient

    init(http: HTTPClient) {
        self.http = http
    }

    /// Optional knobs for ``invoke(toolID:arguments:options:)``.
    public struct Options: Sendable {
        public var workspaceID: String?
        public var agentID: String?
        public var cache: Bool?
        public var idempotencyKey: String?

        public init(
            workspaceID: String? = nil,
            agentID: String? = nil,
            cache: Bool? = nil,
            idempotencyKey: String? = nil
        ) {
            self.workspaceID = workspaceID
            self.agentID = agentID
            self.cache = cache
            self.idempotencyKey = idempotencyKey
        }
    }

    /// Invoke `toolID` with `arguments`. The gateway transparently
    /// caches and audits per CONTRACTS.md.
    @discardableResult
    public func invoke(
        toolID: String,
        arguments: [String: AnyCodableValue],
        options: Options = Options()
    ) async throws -> InvokeResponse {
        let body = InvokeRequest(
            toolId: toolID,
            arguments: arguments,
            workspaceId: options.workspaceID,
            agentId: options.agentID,
            cache: options.cache,
            idempotencyKey: options.idempotencyKey
        )
        return try await http.postJSON(
            "/v1/invoke",
            body: body,
            notFoundCode: PlinthErrorCode.toolNotFound
        )
    }

    /// Convenience: invoke with a `[String: Any]` argument dictionary
    /// (Foundation primitives auto-converted via
    /// ``AnyCodableValue/from(_:)``).
    @discardableResult
    public func invoke(
        toolID: String,
        arguments: [String: Any],
        options: Options = Options()
    ) async throws -> InvokeResponse {
        var converted: [String: AnyCodableValue] = [:]
        for (key, value) in arguments {
            converted[key] = AnyCodableValue.from(value)
        }
        return try await invoke(toolID: toolID, arguments: converted, options: options)
    }

    /// Every registered tool.
    public func list() async throws -> [Tool] {
        let resp: ToolsListResponse = try await http.getJSON("/v1/tools")
        return resp.tools
    }

    /// Fetch a single registered tool by ID.
    public func get(toolID: String) async throws -> Tool {
        return try await http.getJSON(
            "/v1/tools/\(encodePathSegment(toolID))",
            notFoundCode: PlinthErrorCode.toolNotFound
        )
    }

    /// Register a tool with the gateway.
    @discardableResult
    public func register(_ registration: ToolRegistration) async throws -> Tool {
        return try await http.postJSON(
            "/v1/tools/register",
            body: registration
        )
    }

    /// Deregister a tool.
    public func deregister(toolID: String) async throws {
        try await http.delete(
            "/v1/tools/\(encodePathSegment(toolID))",
            notFoundCode: PlinthErrorCode.toolNotFound
        )
    }
}
