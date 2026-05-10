// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.serialization.KSerializer
import kotlinx.serialization.json.Json
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/**
 * Internal request helper bound to a single Plinth service base URL.
 *
 * The [Plinth] facade owns one per backing service (workspace, gateway,
 * identity). Exposed as `internal` so application code goes through the
 * typed sub-clients; tests in the same module can construct one
 * directly for unit testing.
 *
 * Coroutine-friendly: every verb returns from a `suspend fun`, with
 * cancellation propagated to the underlying OkHttp [Call].
 */
internal class HttpClient(
    val baseUrl: HttpUrl,
    private val apiKey: String,
    private val userAgent: String,
    private val httpClient: OkHttpClient,
    val json: Json,
) {
    companion object {
        const val USER_AGENT_DEFAULT = "plinth-sdk-kotlin/0.1.0"

        /** Build a sensible default OkHttp client with a 30s timeout. */
        fun defaultOkHttpClient(timeoutSeconds: Long = 30): OkHttpClient =
            OkHttpClient.Builder()
                .connectTimeout(timeoutSeconds, TimeUnit.SECONDS)
                .readTimeout(timeoutSeconds, TimeUnit.SECONDS)
                .writeTimeout(timeoutSeconds, TimeUnit.SECONDS)
                .build()

        /** Build the kotlinx.serialization JSON instance with the SDK defaults. */
        fun defaultJson(): Json = Json {
            ignoreUnknownKeys = true
            encodeDefaults = false
            explicitNulls = false
        }

        /** Parse `baseUrl` to an [HttpUrl] or throw [PlinthError.InvalidConfig]. */
        fun parseBaseUrl(name: String, baseUrl: String): HttpUrl =
            baseUrl.toHttpUrlOrNull()
                ?: throw PlinthError.InvalidConfig("$name is not a valid URL: $baseUrl")
    }

    // MARK: - Verb helpers (typed)

    suspend fun <T> getJson(
        path: String,
        deserializer: KSerializer<T>,
        query: Map<String, String?> = emptyMap(),
        notFoundCode: String? = null,
    ): T {
        val body = requestRaw("GET", path, query = query, notFoundCode = notFoundCode)
        return decodeJson(deserializer, body)
    }

    suspend fun getBytes(
        path: String,
        query: Map<String, String?> = emptyMap(),
        notFoundCode: String? = null,
    ): ByteArray = requestRaw("GET", path, query = query, notFoundCode = notFoundCode)

    suspend fun <B, T> postJson(
        path: String,
        body: B,
        bodySerializer: KSerializer<B>,
        responseSerializer: KSerializer<T>,
        query: Map<String, String?> = emptyMap(),
        notFoundCode: String? = null,
    ): T {
        val raw = json.encodeToString(bodySerializer, body).toByteArray(Charsets.UTF_8)
        val response = requestRaw(
            "POST",
            path,
            body = raw,
            contentType = "application/json",
            query = query,
            notFoundCode = notFoundCode,
        )
        return decodeJson(responseSerializer, response)
    }

    suspend fun <B, T> putJson(
        path: String,
        body: B,
        bodySerializer: KSerializer<B>,
        responseSerializer: KSerializer<T>,
        query: Map<String, String?> = emptyMap(),
        notFoundCode: String? = null,
    ): T {
        val raw = json.encodeToString(bodySerializer, body).toByteArray(Charsets.UTF_8)
        val response = requestRaw(
            "PUT",
            path,
            body = raw,
            contentType = "application/json",
            query = query,
            notFoundCode = notFoundCode,
        )
        return decodeJson(responseSerializer, response)
    }

    suspend fun <T> putRaw(
        path: String,
        body: ByteArray,
        contentType: String,
        responseSerializer: KSerializer<T>,
        query: Map<String, String?> = emptyMap(),
        notFoundCode: String? = null,
    ): T {
        val response = requestRaw(
            "PUT",
            path,
            body = body,
            contentType = contentType,
            query = query,
            notFoundCode = notFoundCode,
        )
        return decodeJson(responseSerializer, response)
    }

    suspend fun delete(
        path: String,
        query: Map<String, String?> = emptyMap(),
        notFoundCode: String? = null,
    ) {
        requestRaw("DELETE", path, query = query, notFoundCode = notFoundCode)
    }

    // MARK: - Core request

    private suspend fun requestRaw(
        method: String,
        path: String,
        body: ByteArray? = null,
        contentType: String? = null,
        query: Map<String, String?> = emptyMap(),
        notFoundCode: String? = null,
    ): ByteArray = withContext(Dispatchers.IO) {
        val url = buildUrl(path, query)
        val requestBody: RequestBody? = body?.toRequestBody(
            (contentType ?: "application/octet-stream").toMediaType()
        )
        val request = Request.Builder()
            .url(url)
            .method(method, requestBody)
            .header("Authorization", "Bearer $apiKey")
            .header("User-Agent", userAgent)
            .header("Accept", "application/json, application/octet-stream")
            .build()

        val response = try {
            httpClient.newCall(request).await()
        } catch (e: IOException) {
            throw PlinthError.Transport(e)
        }

        response.use {
            val bytes = response.body?.bytes() ?: ByteArray(0)
            if (response.isSuccessful) {
                return@withContext bytes
            }
            val envelope = decodeEnvelope(bytes)
            throw plinthErrorFromEnvelope(
                statusCode = response.code,
                envelope = envelope,
                retryAfterHeader = response.header("Retry-After"),
                fallbackNotFoundCode = notFoundCode,
            )
        }
    }

    // MARK: - URL & body helpers

    private fun buildUrl(path: String, query: Map<String, String?>): HttpUrl {
        val normalised = if (path.startsWith("/")) path.substring(1) else path
        val builder = baseUrl.newBuilder()
        // Add each path segment individually so we preserve pre-encoded
        // segments (KV keys, etc.). Empty segments are dropped.
        for (segment in normalised.split("/")) {
            if (segment.isNotEmpty()) {
                builder.addEncodedPathSegment(segment)
            }
        }
        for ((key, value) in query) {
            if (!value.isNullOrEmpty()) {
                builder.addQueryParameter(key, value)
            }
        }
        return builder.build()
    }

    private fun <T> decodeJson(deserializer: KSerializer<T>, body: ByteArray): T {
        if (body.isEmpty()) {
            // Allow empty bodies for void responses by deserializing
            // null when the type permits — let the caller's serializer
            // surface the error otherwise.
            return try {
                json.decodeFromString(deserializer, "null")
            } catch (e: Exception) {
                throw PlinthError.Decoding("empty response body", e)
            }
        }
        return try {
            json.decodeFromString(deserializer, body.toString(Charsets.UTF_8))
        } catch (e: Exception) {
            throw PlinthError.Decoding(e.message ?: "JSON decode error", e)
        }
    }

    private fun decodeEnvelope(body: ByteArray): PlinthErrorEnvelope? {
        if (body.isEmpty()) return null
        return try {
            json.decodeFromString(
                PlinthErrorEnvelope.serializer(),
                body.toString(Charsets.UTF_8),
            )
        } catch (_: Exception) {
            null
        }
    }
}

// MARK: - OkHttp coroutine adapter

/**
 * Suspend over an OkHttp [Call] and resume when the response arrives.
 *
 * Wires up cancellation so cancelling the coroutine cancels the call.
 */
internal suspend fun Call.await(): Response =
    suspendCancellableCoroutine { cont ->
        enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (cont.isCancelled) return
                cont.resumeWithException(e)
            }

            override fun onResponse(call: Call, response: Response) {
                cont.resume(response) { _ ->
                    response.close()
                }
            }
        })
        cont.invokeOnCancellation {
            try {
                cancel()
            } catch (_: Throwable) {
                // best-effort cancel
            }
        }
    }

// MARK: - Path encoding helpers

/**
 * Percent-encode a single path segment so embedded `/` characters don't
 * become path separators.
 */
internal fun encodePathSegment(s: String): String =
    HttpUrl.Builder().scheme("http").host("x").addPathSegment(s).build()
        .encodedPathSegments.last()

/**
 * Percent-encode a file path while preserving `/` as the segment
 * separator.
 */
internal fun encodeFilePath(p: String): String {
    val trimmed = p.trimStart('/')
    return trimmed.split("/").joinToString("/") { encodePathSegment(it) }
}
