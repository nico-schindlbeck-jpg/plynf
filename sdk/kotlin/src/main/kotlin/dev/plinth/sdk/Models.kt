// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

// Models in this file mirror the Pydantic + TypeScript types defined in
// CONTRACTS.md. Field names use Kotlin conventions (camelCase) and use
// @SerialName to map to the snake_case wire format.

// MARK: - Workspaces

/** Wire-level workspace record returned by the workspace service. */
@Serializable
data class WorkspaceRecord(
    val id: String,
    val name: String,
    @SerialName("created_at") val createdAt: String,
    @SerialName("updated_at") val updatedAt: String,
    val metadata: Map<String, JsonElement>? = null,
)

@Serializable
internal data class WorkspaceListResponse(val workspaces: List<WorkspaceRecord>)

// MARK: - KV

@Serializable
data class KVEntry(
    @SerialName("workspace_id") val workspaceId: String,
    val key: String,
    val value: JsonElement,
    val version: Int,
    @SerialName("created_at") val createdAt: String,
    val deleted: Boolean = false,
    @SerialName("branch_id") val branchId: String? = null,
)

@Serializable
internal data class KVHistoryResponse(val versions: List<KVEntry>)

@Serializable
internal data class KVListResponse(val entries: List<KVEntry>)

@Serializable
internal data class KVSetRequest(val value: JsonElement)

// MARK: - Files

@Serializable
data class FileEntry(
    @SerialName("workspace_id") val workspaceId: String,
    val path: String,
    val size: Long,
    val sha256: String,
    @SerialName("content_type") val contentType: String,
    val version: Int,
    @SerialName("created_at") val createdAt: String,
    val deleted: Boolean = false,
    @SerialName("branch_id") val branchId: String? = null,
)

@Serializable
internal data class FilesListResponse(val files: List<FileEntry>)

// MARK: - Tools

@Serializable
enum class ToolTransport {
    @SerialName("http") HTTP,
    @SerialName("stdio") STDIO,
}

@Serializable
enum class ToolSideEffects {
    @SerialName("none") NONE,
    @SerialName("read") READ,
    @SerialName("write") WRITE,
}

@Serializable
enum class ToolAuthMethod {
    @SerialName("none") NONE,
    @SerialName("bearer") BEARER,
    @SerialName("oauth2") OAUTH2,
}

@Serializable
data class ToolRegistration(
    @SerialName("tool_id") val toolId: String,
    val name: String,
    val description: String,
    val transport: ToolTransport,
    val endpoint: String,
    @SerialName("input_schema") val inputSchema: Map<String, JsonElement> = emptyMap(),
    @SerialName("output_schema") val outputSchema: Map<String, JsonElement> = emptyMap(),
    val idempotent: Boolean? = null,
    @SerialName("side_effects") val sideEffects: ToolSideEffects? = null,
    @SerialName("cache_ttl_seconds") val cacheTtlSeconds: Int? = null,
    @SerialName("auth_method") val authMethod: ToolAuthMethod? = null,
    @SerialName("auth_config") val authConfig: Map<String, JsonElement>? = null,
)

@Serializable
data class Tool(
    @SerialName("tool_id") val toolId: String,
    val name: String,
    val description: String,
    val transport: ToolTransport,
    val endpoint: String,
    @SerialName("input_schema") val inputSchema: Map<String, JsonElement> = emptyMap(),
    @SerialName("output_schema") val outputSchema: Map<String, JsonElement> = emptyMap(),
    val idempotent: Boolean? = null,
    @SerialName("side_effects") val sideEffects: ToolSideEffects? = null,
    @SerialName("cache_ttl_seconds") val cacheTtlSeconds: Int? = null,
    @SerialName("created_at") val createdAt: String,
    @SerialName("updated_at") val updatedAt: String,
)

@Serializable
data class InvokeRequest(
    @SerialName("tool_id") val toolId: String,
    val arguments: Map<String, JsonElement>,
    @SerialName("workspace_id") val workspaceId: String? = null,
    @SerialName("agent_id") val agentId: String? = null,
    val cache: Boolean? = null,
    @SerialName("idempotency_key") val idempotencyKey: String? = null,
)

@Serializable
data class InvokeResponse(
    @SerialName("tool_id") val toolId: String,
    val arguments: Map<String, JsonElement>,
    val result: JsonElement,
    val cached: Boolean,
    @SerialName("duration_ms") val durationMs: Int,
    @SerialName("audit_id") val auditId: String,
    @SerialName("cost_estimate_usd") val costEstimateUsd: Double = 0.0,
)

@Serializable
internal data class ToolsListResponse(val tools: List<Tool>)

// MARK: - Identity

@Serializable
data class TokenIssueRequest(
    @SerialName("agent_id") val agentId: String,
    @SerialName("tenant_id") val tenantId: String? = null,
    val scopes: List<String>,
    @SerialName("workspace_id") val workspaceId: String? = null,
    @SerialName("ttl_seconds") val ttlSeconds: Int? = null,
    val metadata: Map<String, JsonElement>? = null,
)

@Serializable
data class TokenClaims(
    val sub: String,
    val iss: String,
    val aud: String,
    val iat: Long,
    val exp: Long,
    val jti: String,
    @SerialName("agent_id") val agentId: String,
    @SerialName("tenant_id") val tenantId: String,
    @SerialName("workspace_id") val workspaceId: String? = null,
    val scopes: List<String>,
    @SerialName("rate_limit") val rateLimit: Map<String, JsonElement>? = null,
)

@Serializable
data class TokenIssueResponse(
    val token: String,
    val jti: String,
    @SerialName("expires_at") val expiresAt: String,
    val claims: TokenClaims,
)

@Serializable
data class TokenInfo(
    val jti: String,
    @SerialName("agent_id") val agentId: String,
    @SerialName("tenant_id") val tenantId: String,
    @SerialName("issued_at") val issuedAt: String,
    @SerialName("expires_at") val expiresAt: String,
    val revoked: Boolean,
    @SerialName("revoked_at") val revokedAt: String? = null,
    val metadata: Map<String, JsonElement>? = null,
)
