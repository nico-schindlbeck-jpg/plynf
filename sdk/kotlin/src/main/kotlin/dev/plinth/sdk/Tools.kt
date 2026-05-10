// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/**
 * Gateway tool surface: invoke, list, register, deregister.
 *
 * Reachable via [Plinth.tools].
 */
class Tools internal constructor(
    private val http: HttpClient,
) {

    /**
     * Optional knobs for [invoke]. All fields default to null/unset so
     * the gateway applies its own defaults.
     */
    data class Options(
        val workspaceId: String? = null,
        val agentId: String? = null,
        val cache: Boolean? = null,
        val idempotencyKey: String? = null,
    )

    /**
     * Invoke [toolId] with [arguments]. The gateway transparently caches
     * and audits per CONTRACTS.md.
     */
    suspend fun invoke(
        toolId: String,
        arguments: Map<String, JsonElement>,
        options: Options = Options(),
    ): InvokeResponse {
        val body = InvokeRequest(
            toolId = toolId,
            arguments = arguments,
            workspaceId = options.workspaceId,
            agentId = options.agentId,
            cache = options.cache,
            idempotencyKey = options.idempotencyKey,
        )
        return http.postJson(
            path = "/v1/invoke",
            body = body,
            bodySerializer = InvokeRequest.serializer(),
            responseSerializer = InvokeResponse.serializer(),
            notFoundCode = PlinthErrorCode.TOOL_NOT_FOUND,
        )
    }

    /**
     * Convenience: invoke with a `Map<String, Any?>` of Kotlin
     * primitives. Auto-converts to [JsonElement] via the SDK's JSON.
     */
    suspend fun invoke(
        toolId: String,
        arguments: Map<String, Any?>,
        options: Options,
    ): InvokeResponse {
        val json = http.json
        val converted = buildJsonObject {
            for ((key, value) in arguments) {
                put(key, valueToJsonElement(json, value))
            }
        }
        return invoke(toolId, converted.toMap(), options)
    }

    /** Convenience overload with default [Options]. */
    @JvmName("invokeAny")
    suspend fun invoke(
        toolId: String,
        arguments: Map<String, Any?>,
    ): InvokeResponse = invoke(toolId, arguments, Options())

    /** Every registered tool. */
    suspend fun list(): List<Tool> =
        http.getJson(
            path = "/v1/tools",
            deserializer = ToolsListResponse.serializer(),
        ).tools

    /** Fetch a single registered tool by ID. */
    suspend fun get(toolId: String): Tool =
        http.getJson(
            path = "/v1/tools/${encodePathSegment(toolId)}",
            deserializer = Tool.serializer(),
            notFoundCode = PlinthErrorCode.TOOL_NOT_FOUND,
        )

    /** Register a tool with the gateway. */
    suspend fun register(registration: ToolRegistration): Tool =
        http.postJson(
            path = "/v1/tools/register",
            body = registration,
            bodySerializer = ToolRegistration.serializer(),
            responseSerializer = Tool.serializer(),
        )

    /** Deregister a tool. */
    suspend fun deregister(toolId: String) {
        http.delete(
            "/v1/tools/${encodePathSegment(toolId)}",
            notFoundCode = PlinthErrorCode.TOOL_NOT_FOUND,
        )
    }
}

/** Convert a Kotlin Any? primitive into a [JsonElement]. */
private fun valueToJsonElement(json: Json, value: Any?): JsonElement {
    return when (value) {
        null -> JsonPrimitive(null as String?)
        is JsonElement -> value
        is Boolean -> JsonPrimitive(value)
        is Number -> JsonPrimitive(value)
        is String -> JsonPrimitive(value)
        is Map<*, *> -> buildJsonObject {
            for ((k, v) in value) {
                put(k.toString(), valueToJsonElement(json, v))
            }
        }
        is List<*> -> {
            val asList: List<JsonElement> = value.map { valueToJsonElement(json, it) }
            JsonArray(asList)
        }
        else -> JsonPrimitive(value.toString())
    }
}

/**
 * Convert a Kotlin map of Any? values to a `Map<String, JsonElement>`.
 * Convenience for callers building invoke arguments without dealing
 * with `JsonElement` directly.
 */
fun Map<String, Any?>.toJsonArguments(json: Json = HttpClient.defaultJson()): Map<String, JsonElement> {
    val out = mutableMapOf<String, JsonElement>()
    for ((key, value) in this) {
        out[key] = valueToJsonElement(json, value)
    }
    return out
}
