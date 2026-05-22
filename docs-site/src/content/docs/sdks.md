---
title: SDKs
description: Plynf client libraries for Python, TypeScript, Go, Swift, and Kotlin.
section: guides
order: 2
---

Plynf ships first-party SDKs in five languages. All five are at API parity for the v1 surface (workspace, gateway, identity).

| Language    | Package                  | Test count |
|-------------|--------------------------|-----------:|
| Python      | `plinth` (PyPI)          | 1,901      |
| TypeScript  | `@plinth/sdk` (npm)      | 165        |
| Go          | `github.com/.../plinth-go` | 312      |
| Swift       | `Plynf` (SwiftPM)       | 244        |
| Kotlin      | `dev.plinth:plinth-kotlin` | 245      |

Across all 7 SDK test suites: **~2,867** tests passing.

## Install

```bash
# Python
pip install plinth

# TypeScript / Node
npm install @plinth/sdk

# Go
go get github.com/plinth/plinth-go

# Swift Package Manager — Package.swift
# .package(url: "https://github.com/plinth/plinth-swift", from: "1.5.0")

# Kotlin / Gradle
# implementation("dev.plinth:plinth-kotlin:1.5.0")
```

## Hello world

The simplest possible interaction: open a workspace, set a key, read it back.

### Python

```python
from plinth import Plynf

client = Plynf(
    workspace_url="http://localhost:7421",
    gateway_url="http://localhost:7422",
    api_key="...",
)
ws = client.workspace("my-research")
ws.kv.set("topic", "renewable energy")
print(ws.kv.get("topic"))  # "renewable energy"
```

### TypeScript

```typescript
import { Plynf } from "@plinth/sdk";

const client = new Plynf({
  workspaceUrl: "http://localhost:7421",
  gatewayUrl: "http://localhost:7422",
  apiKey: "...",
});
const ws = await client.workspace("my-research");
await ws.kv.set("topic", "renewable energy");
console.log(await ws.kv.get("topic"));
```

### Go

```go
client, _ := plinth.New(plinth.Config{
    WorkspaceURL: "http://localhost:7421",
    GatewayURL:   "http://localhost:7422",
    APIKey:       "...",
})
ws, _ := client.Workspace(ctx, "my-research")
_ = ws.KV.Set(ctx, "topic", "renewable energy")
v, _ := ws.KV.Get(ctx, "topic")
fmt.Println(v)
```

### Swift

```swift
let client = try Plynf(
    workspaceURL: URL(string: "http://localhost:7421")!,
    gatewayURL: URL(string: "http://localhost:7422")!,
    apiKey: "..."
)
let ws = try await client.workspace(name: "my-research")
try await ws.kv.set(key: "topic", value: "renewable energy")
print(try await ws.kv.get(key: "topic") ?? "")
```

### Kotlin

```kotlin
val client = Plynf(
    PlinthConfig(
        workspaceUrl = "http://localhost:7421",
        gatewayUrl = "http://localhost:7422",
        apiKey = "...",
    )
)
val ws = client.workspace("my-research")
ws.kv.set("topic", "renewable energy")
println(ws.kv.get("topic"))
```

## Calling tools

Every SDK exposes `client.invoke(toolId, args)` against the gateway. The gateway handles auth, caching, audit, and rate limiting transparently.

```python
result = client.invoke(
    tool_id="github.search_issues",
    args={"query": "is:open label:bug repo:plinth-dev/plinth"},
)
print(result.cached)        # True on repeated calls
print(result.cost_usd)      # populated by the gateway
```

## Workflows + channels

Each SDK ships durable-workflow and typed-channel surfaces. See the language-specific reference docs for full method signatures, or the `examples/` directory in the repo for end-to-end programs.
