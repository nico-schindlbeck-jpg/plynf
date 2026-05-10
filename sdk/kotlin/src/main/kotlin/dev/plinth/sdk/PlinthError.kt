// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull

/**
 * Stable string identifiers for every Plinth error code emitted by the
 * services. Mirrors the maps in the Python, TypeScript, Go, and Swift
 * SDKs.
 */
object PlinthErrorCode {
    // 400 — validation
    const val INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    const val SCHEMA_VIOLATION = "SCHEMA_VIOLATION"

    // 401 — auth
    const val UNAUTHORIZED = "UNAUTHORIZED"
    const val INVALID_TOKEN = "INVALID_TOKEN"
    const val TOKEN_EXPIRED = "TOKEN_EXPIRED"
    const val TOKEN_REVOKED = "TOKEN_REVOKED"

    // 404 — not found
    const val WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    const val KEY_NOT_FOUND = "KEY_NOT_FOUND"
    const val FILE_NOT_FOUND = "FILE_NOT_FOUND"
    const val SNAPSHOT_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
    const val BRANCH_NOT_FOUND = "BRANCH_NOT_FOUND"
    const val TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    const val SIGNING_KEY_NOT_FOUND = "SIGNING_KEY_NOT_FOUND"

    // 429 — rate limits / cost caps
    const val RATE_LIMITED = "RATE_LIMITED"
    const val COST_CAP_EXCEEDED = "COST_CAP_EXCEEDED"

    // 5xx / client-side
    const val TOOL_INVOCATION_FAILED = "TOOL_INVOCATION_FAILED"
    const val INVALID_CONFIG = "INVALID_CONFIG"
    const val CONNECTION_ERROR = "CONNECTION_ERROR"
    const val INTERNAL_ERROR = "INTERNAL_ERROR"
    const val IDENTITY_NOT_CONFIGURED = "IDENTITY_NOT_CONFIGURED"
}

/**
 * Single typed-error hierarchy returned by every Plinth SDK call.
 *
 * Sealed so callers can `when` over the full set without forgetting a
 * case. Each subclass exposes the stable [code] so log dashboards can
 * group on a single string identifier.
 *
 * ```kotlin
 * try {
 *     ws.kv.get<String>("missing")
 * } catch (_: PlinthError.KeyNotFound) {
 *     // recover
 * } catch (e: PlinthError.Server) {
 *     println("server: ${e.statusCode} ${e.code}: ${e.message}")
 * }
 * ```
 */
sealed class PlinthError(
    message: String,
    cause: Throwable? = null,
) : Exception(message, cause) {

    /** The stable wire code (matches the envelope's `error.code`). */
    abstract val code: String

    /** The SDK was constructed with invalid configuration. */
    class InvalidConfig(message: String) : PlinthError(message) {
        override val code = PlinthErrorCode.INVALID_CONFIG
    }

    /** 401 — bearer token rejected. */
    class Unauthorized(message: String) : PlinthError(message) {
        override val code = PlinthErrorCode.UNAUTHORIZED
    }

    /** 404 with a recognised resource code that doesn't have a dedicated subclass. */
    class NotFound(override val code: String, message: String) : PlinthError(message)

    /** 404 — workspace not found. */
    class WorkspaceNotFound(message: String = "workspace not found") : PlinthError(message) {
        override val code = PlinthErrorCode.WORKSPACE_NOT_FOUND
    }

    /** 404 — KV key not found. */
    class KeyNotFound(message: String = "key not found") : PlinthError(message) {
        override val code = PlinthErrorCode.KEY_NOT_FOUND
    }

    /** 404 — file not found. */
    class FileNotFound(message: String = "file not found") : PlinthError(message) {
        override val code = PlinthErrorCode.FILE_NOT_FOUND
    }

    /** 404 — tool not registered. */
    class ToolNotFound(message: String = "tool not found") : PlinthError(message) {
        override val code = PlinthErrorCode.TOOL_NOT_FOUND
    }

    /**
     * 429 — the caller is being throttled. [retryAfterSeconds] is the
     * server's hint (from the envelope details or the `Retry-After`
     * header), or null when no hint was provided.
     */
    class RateLimited(val retryAfterSeconds: Double? = null) :
        PlinthError("rate limited" + (retryAfterSeconds?.let { " (retry after ${it}s)" } ?: "")) {
        override val code = PlinthErrorCode.RATE_LIMITED
    }

    /** 429 — a tenant cost cap or quota was exceeded. */
    class QuotaExceeded(val quota: String) : PlinthError("quota exceeded: $quota") {
        override val code = PlinthErrorCode.COST_CAP_EXCEEDED
    }

    /** Generic 4xx/5xx error that didn't fit the dedicated subclasses. */
    class Server(
        val statusCode: Int,
        override val code: String,
        message: String,
    ) : PlinthError(message)

    /** JSON decoding failed on a 2xx response. */
    class Decoding(message: String, cause: Throwable? = null) : PlinthError(message, cause) {
        override val code = PlinthErrorCode.INTERNAL_ERROR
    }

    /** Network/transport error before a status code was seen. */
    class Transport(cause: Throwable) : PlinthError("transport error: ${cause.message}", cause) {
        override val code = PlinthErrorCode.CONNECTION_ERROR
    }

    /** [Plinth.identity] was accessed without [PlinthConfig.identityUrl]. */
    class IdentityNotConfigured :
        PlinthError("identity service not configured: pass identityUrl to PlinthConfig") {
        override val code = PlinthErrorCode.IDENTITY_NOT_CONFIGURED
    }
}

/**
 * Decoded form of the standard `{ "error": { ... } }` envelope returned
 * by every Plinth service.
 */
@Serializable
data class PlinthErrorEnvelope(
    val error: ErrorPayload,
) {
    @Serializable
    data class ErrorPayload(
        val code: String,
        val message: String = "",
        val details: Map<String, JsonElement>? = null,
    )
}

/**
 * Map an HTTP failure response (status + envelope + headers) to the
 * most specific [PlinthError] subclass.
 *
 * Pulled out as a top-level function so it's unit-testable in isolation.
 */
fun plinthErrorFromEnvelope(
    statusCode: Int,
    envelope: PlinthErrorEnvelope?,
    retryAfterHeader: String?,
    fallbackNotFoundCode: String? = null,
): PlinthError {
    val envelopeCode = envelope?.error?.code.orEmpty()
    val envelopeMessage = envelope?.error?.message.orEmpty()

    val code = when {
        envelopeCode.isNotEmpty() -> envelopeCode
        statusCode == 404 && !fallbackNotFoundCode.isNullOrEmpty() -> fallbackNotFoundCode
        else -> statusToCode[statusCode] ?: PlinthErrorCode.INTERNAL_ERROR
    }

    val message = envelopeMessage.ifEmpty { "HTTP $statusCode" }

    return when (code) {
        PlinthErrorCode.WORKSPACE_NOT_FOUND -> PlinthError.WorkspaceNotFound(message)
        PlinthErrorCode.KEY_NOT_FOUND -> PlinthError.KeyNotFound(message)
        PlinthErrorCode.FILE_NOT_FOUND -> PlinthError.FileNotFound(message)
        PlinthErrorCode.TOOL_NOT_FOUND -> PlinthError.ToolNotFound(message)
        PlinthErrorCode.UNAUTHORIZED,
        PlinthErrorCode.INVALID_TOKEN,
        PlinthErrorCode.TOKEN_EXPIRED,
        PlinthErrorCode.TOKEN_REVOKED,
        -> PlinthError.Unauthorized(message)
        PlinthErrorCode.RATE_LIMITED -> PlinthError.RateLimited(
            retryAfterSeconds = parseRetryAfter(retryAfterHeader, envelope)
        )
        PlinthErrorCode.COST_CAP_EXCEEDED -> {
            val quota = envelope?.error?.details?.get("limit_type")?.let { el ->
                (el as? JsonPrimitive)?.content
            } ?: "cost"
            PlinthError.QuotaExceeded(quota)
        }
        else ->
            if (statusCode == 404)
                PlinthError.NotFound(code, message)
            else
                PlinthError.Server(statusCode, code, message)
    }
}

/** Default fallback table when the server's envelope is missing a code. */
private val statusToCode: Map<Int, String> = mapOf(
    400 to PlinthErrorCode.INVALID_ARGUMENTS,
    401 to PlinthErrorCode.UNAUTHORIZED,
    429 to PlinthErrorCode.RATE_LIMITED,
)

internal fun parseRetryAfter(header: String?, envelope: PlinthErrorEnvelope?): Double? {
    envelope?.error?.details?.get("retry_after_seconds")?.let { el ->
        val prim = el as? JsonPrimitive ?: return@let
        prim.doubleOrNull?.let { return it }
        prim.content.toDoubleOrNull()?.let { return it }
    }
    return header?.toDoubleOrNull()
}
