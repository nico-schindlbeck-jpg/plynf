# Plynf SDK for Swift

Idiomatic Swift client for [Plynf](https://github.com/plinth/plinth) — workspaces, KV, files, tools, and identity. Built with Swift Concurrency (`async/await`, `Sendable`) for iOS 16+ and macOS 13+. v0.1 covers the minimum-viable mobile surface; the Python/TypeScript SDKs remain the reference for advanced features (LLM facade, workflows, multi-region).

## Status

Version `v0.1`. Foundation-only — no external dependencies.

## Installation

In Xcode: **File → Add Packages…**, paste the repository URL, and select the `Plynf` library.

In a `Package.swift`:

```swift
dependencies: [
    .package(url: "https://github.com/plinth/plinth", from: "0.1.0"),
],
targets: [
    .target(name: "MyApp", dependencies: [
        .product(name: "Plynf", package: "plinth"),
    ]),
]
```

Requires Swift 5.9 / Xcode 15 or later. Supported on iOS 16+ and macOS 13+.

## Quickstart

```swift
import Plynf

let client = try Plynf(
    workspaceURL: "http://localhost:7421",
    gatewayURL:   "http://localhost:7422",
    identityURL:  "http://localhost:7425",   // optional
    apiKey:       "local-dev"
)

// Get-or-create a workspace.
let ws = try await client.workspace(name: "research-task-1")

// Versioned KV writes.
try await ws.kv.set(key: "topic", value: "renewable energy")
let topic: String = try await ws.kv.get(key: "topic")

// Files.
try await ws.files.write(path: "report.md", text: "# Report\n…")
let body = try await ws.files.readText(path: "report.md")

// Tool gateway.
let result = try await client.tools.invoke(
    toolID: "web.fetch",
    arguments: ["url": "mock://example"],
    options: .init(workspaceID: ws.id)
)
print(result.cached, result.result)

// Identity (mint short-lived capability tokens).
let token = try await client.identity.issueToken(
    agentID: "my-agent",
    scopes: ["tool:web.fetch:read"],
    ttlSeconds: 3600
)
let claims = try await client.identity.verifyToken(token.token)
```

## Surface

| Feature                                  | Swift SDK | Python SDK | TS SDK | Go SDK |
|------------------------------------------|-----------|------------|--------|--------|
| Workspaces (get-or-create, list, delete) | yes       | yes        | yes    | yes    |
| KV (versioned set/get/history)           | yes       | yes        | yes    | yes    |
| Files (versioned blob storage)           | yes       | yes        | yes    | yes    |
| Tools (`invoke`, list)                   | yes       | yes        | yes    | yes    |
| Identity (token issue/verify/revoke)     | yes       | yes        | yes    | yes    |
| Snapshots + branches                     | no        | yes        | yes    | yes    |
| Channels                                 | no        | yes        | yes    | yes    |
| Workflows                                | no        | yes        | yes    | yes    |
| LLM facade                               | no        | yes        | yes    | no     |
| Multi-region failover                    | no        | yes        | yes    | no     |

## v0.1 Limitations

- No snapshots/branches/channels/workflows yet. Use the Python/TS SDKs from a backend for those.
- No streaming responses (SSE) yet — tool invocation is request/response.
- No built-in retry/backoff loop; callers should implement their own.
- No multi-region failover. The first request always goes to the configured URL.

## Error handling

Every SDK call throws `PlinthError`, a typed enum. Common cases have dedicated cases so you can branch without unpacking:

```swift
do {
    let _: String = try await ws.kv.get(key: "missing")
} catch PlinthError.keyNotFound {
    // recover
} catch PlinthError.rateLimited(let retryAfter) {
    // sleep `retryAfter` and retry
} catch PlinthError.server(let status, let code, let message) {
    print("server error \(status) \(code): \(message)")
}
```

The `code` property exposes the stable wire code (`"WORKSPACE_NOT_FOUND"`, etc.) for log dashboards.

## Concurrency model

`Plynf` is a value-type `struct` and conforms to `Sendable`. Every sub-client also conforms to `Sendable`, so you can freely share an instance across `Task`s and actors. Internally it wraps a `URLSession` (which is already thread-safe).

## Testing

The package ships with an in-process URLProtocol-based mock (`MockURLProtocol` in `Tests/PlinthTests/MockServer.swift`) that intercepts requests sent through a session configured with `MockURLProtocol.self`. See the test suite for usage patterns.

```bash
swift test
```

Tests require a full Swift toolchain (Xcode or swift.org distribution); the bare Command Line Tools don't ship XCTest. The library itself builds without Xcode (`swift build` works with CLT).

## License

Apache 2.0. See [LICENSE](../../LICENSE).
