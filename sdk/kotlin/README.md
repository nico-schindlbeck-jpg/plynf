# Plinth SDK for Kotlin

Idiomatic Kotlin client for [Plinth](https://github.com/plinth/plinth) — workspaces, KV, files, tools, and identity. Coroutine-friendly (`suspend fun` everywhere), JSON via kotlinx.serialization, transport via OkHttp. Suitable for Android (API 26+) and JVM 17+ services.

## Status

Version `v0.1.0`. Three runtime dependencies: kotlinx.coroutines, kotlinx.serialization, OkHttp.

## Installation

Once published to Maven Central:

```kotlin
// build.gradle.kts
dependencies {
    implementation("dev.plinth:plinth-sdk:0.1.0")
}
```

Building from source:

```bash
cd sdk/kotlin
./gradlew build
```

Requires JDK 17. On Android, also requires `minSdk = 26` (because OkHttp 4.12 sets that floor).

## Quickstart

```kotlin
import dev.plinth.sdk.Plinth
import dev.plinth.sdk.PlinthConfig
import dev.plinth.sdk.Tools
import kotlinx.coroutines.runBlocking

fun main() = runBlocking {
    val client = Plinth(PlinthConfig(
        workspaceUrl = "http://localhost:7421",
        gatewayUrl   = "http://localhost:7422",
        identityUrl  = "http://localhost:7425",   // optional
        apiKey       = "local-dev",
    ))

    // Get-or-create a workspace.
    val ws = client.workspace("research-task-1")

    // Versioned KV writes.
    ws.kv.set("topic", "renewable energy")
    val topic: String = ws.kv.get<String>("topic")

    // Files.
    ws.files.write("report.md", "# Report\n…")
    val body = ws.files.readText("report.md")

    // Tool gateway.
    val result = client.tools.invoke(
        toolId = "web.fetch",
        arguments = mapOf("url" to "mock://example"),
        options = Tools.Options(workspaceId = ws.id),
    )
    println("cached=${result.cached} result=${result.result}")

    // Identity (mint short-lived capability tokens).
    val token = client.identity.issueToken(
        agentId    = "my-agent",
        scopes     = listOf("tool:web.fetch:read"),
        ttlSeconds = 3600,
    )
    val claims = client.identity.verifyToken(token.token)
}
```

All methods are `suspend fun` — call them from a coroutine. On Android, use `viewModelScope.launch { … }` or `lifecycleScope.launch { … }`.

## Surface

| Feature                                  | Kotlin SDK | Python SDK | TS SDK | Go SDK |
|------------------------------------------|------------|------------|--------|--------|
| Workspaces (get-or-create, list, delete) | yes        | yes        | yes    | yes    |
| KV (versioned set/get/history)           | yes        | yes        | yes    | yes    |
| Files (versioned blob storage)           | yes        | yes        | yes    | yes    |
| Tools (`invoke`, list, register)         | yes        | yes        | yes    | yes    |
| Identity (token issue/verify/revoke)     | yes        | yes        | yes    | yes    |
| Snapshots + branches                     | no         | yes        | yes    | yes    |
| Channels                                 | no         | yes        | yes    | yes    |
| Workflows                                | no         | yes        | yes    | yes    |
| LLM facade                               | no         | yes        | yes    | no     |
| Multi-region failover                    | no         | yes        | yes    | no     |

## v0.1 Limitations

- No snapshots/branches/channels/workflows yet. Use the Python/TS SDKs from a backend for those.
- No streaming responses (SSE) yet — tool invocation is request/response.
- No built-in retry/backoff; supply your own `OkHttpClient` with an interceptor if you need it.
- No multi-region failover. The first request always goes to the configured URL.

## Error handling

Every SDK call throws a subclass of the sealed `PlinthError`. Common cases are dedicated subclasses; less common ones land on `PlinthError.Server`:

```kotlin
try {
    ws.kv.get<String>("missing")
} catch (_: PlinthError.KeyNotFound) {
    // recover
} catch (e: PlinthError.RateLimited) {
    delay((e.retryAfterSeconds ?: 1.0).times(1000).toLong())
} catch (e: PlinthError.Server) {
    println("server error ${e.statusCode} ${e.code}: ${e.message}")
}
```

Every subclass exposes the stable wire `code` (`"WORKSPACE_NOT_FOUND"`, etc.) for log dashboards.

## Concurrency model

The client is safe to share across coroutines. Internally it wraps a single `OkHttpClient` (which is already thread-safe). Pass a custom `OkHttpClient` via `PlinthConfig.okHttpClient` to wire up your own interceptors, retries, or HTTP/2 settings.

## Testing

The test suite uses `MockWebServer` for HTTP-level fakes:

```bash
./gradlew test
```

The test count is ≥ 50 covering construction validation, workspace get-or-create, KV round-trips, file upload/download, tool invocation, identity token issue/verify, error mapping (401 → Unauthorized, 404 → KeyNotFound/FileNotFound/ToolNotFound, 429 → RateLimited), path encoding, and JSON round-trips.

## Android notes

- `minSdk = 26` is required by the OkHttp 4.x line. If you need older Android support, swap OkHttp for OkHttp 3.x in your `dependencies` block.
- Network requests run on a background thread by default (the SDK uses `Dispatchers.IO` internally). Don't block the main thread.
- Add the standard internet permission to your `AndroidManifest.xml`:
  ```xml
  <uses-permission android:name="android.permission.INTERNET" />
  ```

## License

Apache 2.0. See [LICENSE](../../LICENSE).
