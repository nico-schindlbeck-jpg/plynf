// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient

/**
 * Configuration bundle for [Plinth].
 *
 * At minimum [workspaceUrl], [gatewayUrl], and [apiKey] must be set.
 * [identityUrl] is optional — leave null to disable token
 * issuance/verification.
 *
 * @property workspaceUrl base URL of the workspace service.
 * @property gatewayUrl base URL of the gateway service.
 * @property identityUrl base URL of the identity service. Optional.
 * @property apiKey bearer token sent on every request. In local dev,
 *   any non-empty string is accepted.
 * @property timeoutSeconds per-request timeout in seconds.
 * @property userAgent `User-Agent` header value.
 * @property okHttpClient optional custom OkHttp client. When null, a
 *   sensible default with [timeoutSeconds] timeouts is constructed.
 * @property json optional custom JSON instance.
 */
data class PlinthConfig(
    val workspaceUrl: String = DEFAULT_WORKSPACE_URL,
    val gatewayUrl: String = DEFAULT_GATEWAY_URL,
    val identityUrl: String? = null,
    val apiKey: String,
    val timeoutSeconds: Long = 30,
    val userAgent: String = HttpClient.USER_AGENT_DEFAULT,
    val okHttpClient: OkHttpClient? = null,
    val json: Json? = null,
) {
    companion object {
        const val DEFAULT_WORKSPACE_URL = "http://localhost:7421"
        const val DEFAULT_GATEWAY_URL = "http://localhost:7422"
        const val DEFAULT_IDENTITY_URL = "http://localhost:7425"
    }
}

/**
 * Top-level entry point for the Plinth SDK.
 *
 * Construct one [Plinth] per app; it owns one [HttpClient] per backing
 * service (workspace, gateway, identity) and exposes typed sub-clients
 * reachable via the public properties.
 *
 * ```kotlin
 * val client = Plinth(PlinthConfig(
 *     workspaceUrl = "http://localhost:7421",
 *     gatewayUrl   = "http://localhost:7422",
 *     identityUrl  = "http://localhost:7425",   // optional
 *     apiKey       = "local-dev",
 * ))
 *
 * val ws = client.workspace("research-task-1")
 * ws.kv.set("topic", "renewable energy")
 * ```
 */
class Plinth(private val config: PlinthConfig) {

    private val httpClientImpl: OkHttpClient
    private val jsonImpl: Json

    internal val workspaceHttp: HttpClient
    internal val gatewayHttp: HttpClient
    private val identityHttp: HttpClient?

    /** Tool gateway client. */
    val tools: Tools

    init {
        if (config.apiKey.isBlank()) {
            throw PlinthError.InvalidConfig(
                "apiKey is required (in local dev, any non-empty string works)"
            )
        }
        this.httpClientImpl = config.okHttpClient
            ?: HttpClient.defaultOkHttpClient(config.timeoutSeconds)
        this.jsonImpl = config.json ?: HttpClient.defaultJson()

        val wsBase = HttpClient.parseBaseUrl("workspaceUrl", config.workspaceUrl)
        val gwBase = HttpClient.parseBaseUrl("gatewayUrl", config.gatewayUrl)

        this.workspaceHttp = HttpClient(
            baseUrl = wsBase,
            apiKey = config.apiKey,
            userAgent = config.userAgent,
            httpClient = httpClientImpl,
            json = jsonImpl,
        )
        this.gatewayHttp = HttpClient(
            baseUrl = gwBase,
            apiKey = config.apiKey,
            userAgent = config.userAgent,
            httpClient = httpClientImpl,
            json = jsonImpl,
        )
        this.identityHttp = config.identityUrl?.takeIf { it.isNotBlank() }?.let { url ->
            val base = HttpClient.parseBaseUrl("identityUrl", url)
            HttpClient(
                baseUrl = base,
                apiKey = config.apiKey,
                userAgent = config.userAgent,
                httpClient = httpClientImpl,
                json = jsonImpl,
            )
        }
        this.tools = Tools(gatewayHttp)
    }

    /**
     * Identity service client.
     *
     * Throws [PlinthError.IdentityNotConfigured] when accessed without
     * `identityUrl` in the [PlinthConfig].
     */
    val identity: Identity
        get() = identityHttp?.let { Identity(it) }
            ?: throw PlinthError.IdentityNotConfigured()

    /** True when [identity] is available. */
    val hasIdentity: Boolean get() = identityHttp != null

    // MARK: - Workspaces

    /**
     * Get-or-create a workspace by name.
     *
     * Lists every workspace and matches on `name`. If none exists, one
     * is created. Equivalent to the Python SDK's
     * `client.workspace(name)`.
     */
    suspend fun workspace(name: String): Workspace {
        findWorkspaceByName(name)?.let { return Workspace(it, workspaceHttp) }
        val record = workspaceHttp.postJson(
            path = "/v1/workspaces",
            body = WorkspaceCreateBody(name),
            bodySerializer = WorkspaceCreateBody.serializer(),
            responseSerializer = WorkspaceRecord.serializer(),
        )
        return Workspace(record, workspaceHttp)
    }

    /** Fetch a workspace by stable ID. */
    suspend fun getWorkspace(id: String): Workspace {
        val record = workspaceHttp.getJson(
            path = "/v1/workspaces/${encodePathSegment(id)}",
            deserializer = WorkspaceRecord.serializer(),
            notFoundCode = PlinthErrorCode.WORKSPACE_NOT_FOUND,
        )
        return Workspace(record, workspaceHttp)
    }

    /** List every workspace visible to this client. */
    suspend fun listWorkspaces(): List<WorkspaceRecord> =
        workspaceHttp.getJson(
            path = "/v1/workspaces",
            deserializer = WorkspaceListResponse.serializer(),
        ).workspaces

    /** Delete a workspace by ID. */
    suspend fun deleteWorkspace(id: String) {
        workspaceHttp.delete(
            "/v1/workspaces/${encodePathSegment(id)}",
            notFoundCode = PlinthErrorCode.WORKSPACE_NOT_FOUND,
        )
    }

    // MARK: - Internal helpers

    private suspend fun findWorkspaceByName(name: String): WorkspaceRecord? {
        val all = listWorkspaces()
        val matches = all.filter { it.name == name }
        if (matches.isEmpty()) return null
        // Deterministic tiebreak: prefer the most recently updated.
        return matches.maxByOrNull { it.updatedAt }
    }
}

@Serializable
internal data class WorkspaceCreateBody(val name: String)
