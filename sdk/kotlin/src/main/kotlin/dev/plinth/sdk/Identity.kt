// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/**
 * Identity service surface: token issuance, verification, revocation.
 *
 * Construct via [Plinth.identity]. The accessor throws
 * [PlinthError.IdentityNotConfigured] when the SDK was initialised
 * without an `identityUrl`.
 */
class Identity internal constructor(
    private val http: HttpClient,
) {

    /**
     * Mint a new capability token for [agentId].
     *
     * @param agentId the agent the token authorises.
     * @param scopes scope strings (e.g. `"tool:web.fetch:read"`).
     * @param workspaceId optional workspace scope.
     * @param ttlSeconds how long the token should remain valid.
     * @param tenantId optional tenant ID; defaults to the service-side
     *   default.
     * @param metadata arbitrary key/value metadata stamped on the token.
     */
    suspend fun issueToken(
        agentId: String,
        scopes: List<String>,
        workspaceId: String? = null,
        ttlSeconds: Int? = null,
        tenantId: String? = null,
        metadata: Map<String, JsonElement>? = null,
    ): TokenIssueResponse {
        val body = TokenIssueRequest(
            agentId = agentId,
            tenantId = tenantId,
            scopes = scopes,
            workspaceId = workspaceId,
            ttlSeconds = ttlSeconds,
            metadata = metadata,
        )
        return http.postJson(
            path = "/v1/tokens",
            body = body,
            bodySerializer = TokenIssueRequest.serializer(),
            responseSerializer = TokenIssueResponse.serializer(),
        )
    }

    /**
     * Validate a [token] and return its decoded claims.
     *
     * Throws [PlinthError.Unauthorized] on invalid/expired/revoked tokens.
     */
    suspend fun verifyToken(token: String): TokenClaims =
        http.postJson(
            path = "/v1/tokens/verify",
            body = VerifyTokenRequest(token),
            bodySerializer = VerifyTokenRequest.serializer(),
            responseSerializer = TokenClaims.serializer(),
        )

    /** Revoke a token by its JTI. */
    suspend fun revokeToken(jti: String) {
        http.delete("/v1/tokens/${encodePathSegment(jti)}/revoke")
    }

    /** Fetch token metadata (no secret) by JTI. */
    suspend fun tokenInfo(jti: String): TokenInfo =
        http.getJson(
            path = "/v1/tokens/${encodePathSegment(jti)}",
            deserializer = TokenInfo.serializer(),
        )
}

@Serializable
private data class VerifyTokenRequest(val token: String)
