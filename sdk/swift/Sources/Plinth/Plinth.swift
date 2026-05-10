// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors
//
// Public surface for the Plinth Swift SDK.
//
// Construct one ``Plinth`` per app; it owns one ``HTTPClient`` per
// backing service (workspace, gateway, identity) and exposes typed
// sub-clients reachable via the public computed properties.
//
//     let client = try Plinth(
//         workspaceURL: "http://localhost:7421",
//         gatewayURL:   "http://localhost:7422",
//         identityURL:  "http://localhost:7425",   // optional
//         apiKey:       "local-dev"
//     )
//
//     let ws = try await client.workspace(name: "research-task-1")
//     try await ws.kv.set(key: "topic", value: "renewable energy")

import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Default endpoints — match the docker-compose / CONTRACTS.md defaults.
public enum PlinthDefaults {
    public static let workspaceURL = "http://localhost:7421"
    public static let gatewayURL = "http://localhost:7422"
    public static let identityURL = "http://localhost:7425"
    public static let timeout: TimeInterval = 30
    public static let userAgent = "plinth-sdk-swift/0.1.0"
}

/// Top-level entry point for the Plinth SDK.
///
/// The type is a value-type struct rather than an actor: every
/// sub-client is `Sendable` and stateless modulo its `URLSession`, so
/// the client itself can be freely passed across tasks without
/// serialising access. If you want serialised access for a particular
/// sub-client, wrap it in an `actor` of your own.
public struct Plinth: Sendable {
    /// HTTP client for the workspace service.
    public let workspaceHTTP: HTTPClient

    /// HTTP client for the gateway service.
    public let gatewayHTTP: HTTPClient

    /// HTTP client for the identity service. Nil when `identityURL`
    /// was not supplied at construction.
    public let identityHTTP: HTTPClient?

    /// Tool gateway client.
    public var tools: Tools {
        Tools(http: gatewayHTTP)
    }

    /// Identity client. Throws ``PlinthError/identityNotConfigured``
    /// when accessed without a configured identity URL.
    public var identity: Identity {
        get throws {
            guard let http = identityHTTP else {
                throw PlinthError.identityNotConfigured
            }
            return Identity(http: http)
        }
    }

    /// Construct a fully-wired client.
    ///
    /// - Parameters:
    ///   - workspaceURL: Workspace service base URL.
    ///   - gatewayURL: Gateway service base URL.
    ///   - identityURL: Identity service base URL. Optional — leave nil
    ///     to disable token issuance/verification.
    ///   - apiKey: Bearer token sent on every request. In local dev,
    ///     any non-empty string is accepted.
    ///   - timeout: Per-request timeout in seconds.
    ///   - userAgent: `User-Agent` header value.
    ///   - urlSession: Underlying `URLSession`. Tests inject a session
    ///     with a custom `URLProtocol`.
    public init(
        workspaceURL: String = PlinthDefaults.workspaceURL,
        gatewayURL: String = PlinthDefaults.gatewayURL,
        identityURL: String? = nil,
        apiKey: String,
        timeout: TimeInterval = PlinthDefaults.timeout,
        userAgent: String = PlinthDefaults.userAgent,
        urlSession: URLSession = .shared
    ) throws {
        guard !apiKey.isEmpty else {
            throw PlinthError.invalidConfig(
                "apiKey is required (in local dev, any non-empty string works)"
            )
        }
        guard let workspaceBase = URL(string: workspaceURL), workspaceBase.scheme != nil else {
            throw PlinthError.invalidConfig("workspaceURL is not a valid URL: \(workspaceURL)")
        }
        guard let gatewayBase = URL(string: gatewayURL), gatewayBase.scheme != nil else {
            throw PlinthError.invalidConfig("gatewayURL is not a valid URL: \(gatewayURL)")
        }

        self.workspaceHTTP = HTTPClient(
            baseURL: workspaceBase,
            apiKey: apiKey,
            userAgent: userAgent,
            timeout: timeout,
            session: urlSession
        )
        self.gatewayHTTP = HTTPClient(
            baseURL: gatewayBase,
            apiKey: apiKey,
            userAgent: userAgent,
            timeout: timeout,
            session: urlSession
        )

        if let identityURL = identityURL, !identityURL.isEmpty {
            guard let base = URL(string: identityURL), base.scheme != nil else {
                throw PlinthError.invalidConfig("identityURL is not a valid URL: \(identityURL)")
            }
            self.identityHTTP = HTTPClient(
                baseURL: base,
                apiKey: apiKey,
                userAgent: userAgent,
                timeout: timeout,
                session: urlSession
            )
        } else {
            self.identityHTTP = nil
        }
    }

    // MARK: - Workspaces

    /// Get-or-create a workspace by name.
    ///
    /// Lists every workspace and matches on `name`. If none exists, one
    /// is created. Equivalent to the Python SDK's
    /// `client.workspace(name)`.
    public func workspace(name: String) async throws -> Workspace {
        if let existing = try await findWorkspaceByName(name) {
            return Workspace(record: existing, http: workspaceHTTP)
        }
        struct CreateBody: Encodable {
            let name: String
        }
        let record: WorkspaceRecord = try await workspaceHTTP.postJSON(
            "/v1/workspaces",
            body: CreateBody(name: name)
        )
        return Workspace(record: record, http: workspaceHTTP)
    }

    /// Fetch a workspace by stable ID.
    public func getWorkspace(id: String) async throws -> Workspace {
        let record: WorkspaceRecord = try await workspaceHTTP.getJSON(
            "/v1/workspaces/\(encodePathSegment(id))",
            notFoundCode: PlinthErrorCode.workspaceNotFound
        )
        return Workspace(record: record, http: workspaceHTTP)
    }

    /// List every workspace visible to this client.
    public func listWorkspaces() async throws -> [WorkspaceRecord] {
        let resp: WorkspaceListResponseInternal = try await workspaceHTTP.getJSON("/v1/workspaces")
        return resp.workspaces
    }

    /// Delete a workspace by ID.
    public func deleteWorkspace(id: String) async throws {
        try await workspaceHTTP.delete(
            "/v1/workspaces/\(encodePathSegment(id))",
            notFoundCode: PlinthErrorCode.workspaceNotFound
        )
    }

    // MARK: - Internal helpers

    private func findWorkspaceByName(_ name: String) async throws -> WorkspaceRecord? {
        let all = try await listWorkspaces()
        let matches = all.filter { $0.name == name }
        guard !matches.isEmpty else { return nil }
        // Deterministic tiebreak: prefer the most recently updated.
        return matches.max { $0.updatedAt < $1.updatedAt }
    }
}
