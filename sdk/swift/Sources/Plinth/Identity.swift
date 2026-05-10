// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation

/// Identity service surface: token issuance, verification, revocation.
///
/// Construct via ``Plinth/identity`` — returns nil when the SDK was
/// initialised without an `identityURL`.
public struct Identity: Sendable {
    let http: HTTPClient

    init(http: HTTPClient) {
        self.http = http
    }

    /// Mint a new capability token for `agentID`.
    ///
    /// - Parameters:
    ///   - agentID: The agent the token authorises.
    ///   - scopes: Scope strings (e.g. `"tool:web.fetch:read"`).
    ///   - workspaceID: Optional workspace scope.
    ///   - ttlSeconds: How long the token should remain valid.
    ///   - tenantID: Optional tenant ID; defaults to the service-side
    ///     default.
    ///   - metadata: Arbitrary key/value metadata stamped on the token.
    @discardableResult
    public func issueToken(
        agentID: String,
        scopes: [String],
        workspaceID: String? = nil,
        ttlSeconds: Int? = nil,
        tenantID: String? = nil,
        metadata: [String: AnyCodableValue]? = nil
    ) async throws -> TokenIssueResponse {
        let body = TokenIssueRequest(
            agentId: agentID,
            tenantId: tenantID,
            scopes: scopes,
            workspaceId: workspaceID,
            ttlSeconds: ttlSeconds,
            metadata: metadata
        )
        return try await http.postJSON("/v1/tokens", body: body)
    }

    /// Validate a token and return its decoded claims.
    ///
    /// Throws ``PlinthError/unauthorized(_:)`` on invalid/expired/revoked
    /// tokens.
    public func verifyToken(_ token: String) async throws -> TokenClaims {
        struct VerifyRequest: Encodable {
            let token: String
        }
        return try await http.postJSON(
            "/v1/tokens/verify",
            body: VerifyRequest(token: token)
        )
    }

    /// Revoke a token by its JTI.
    public func revokeToken(jti: String) async throws {
        try await http.delete("/v1/tokens/\(encodePathSegment(jti))/revoke")
    }

    /// Fetch token metadata (no secret) by JTI.
    public func tokenInfo(jti: String) async throws -> TokenInfo {
        return try await http.getJSON("/v1/tokens/\(encodePathSegment(jti))")
    }

    /// The public JWKS used to verify identity-signed tokens locally.
    public func jwks() async throws -> AnyCodableValue {
        let data = try await http.getData("/v1/.well-known/jwks.json")
        do {
            return try http.decoder.decode(AnyCodableValue.self, from: data)
        } catch {
            throw PlinthError.decoding(error.localizedDescription)
        }
    }
}
