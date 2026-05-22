# Plynf SDK for Go

Idiomatic Go client for [Plynf](https://github.com/plinth/plinth) — workspaces, tools, identity, workers, and workflows. Built for the cloud-native ecosystem (Kubernetes operators, Go-based services) and brings parity with the Python and TypeScript SDKs for the v1.0 core surface.

## Status

Version `v0.1`. Stdlib-only — no external dependencies.

## Installation

```bash
go get github.com/plinth/sdk-go
```

Requires Go 1.22 or later.

## Quickstart

```go
package main

import (
    "context"
    "fmt"
    "log"

    "github.com/plinth/sdk-go/plinth"
)

func main() {
    ctx := context.Background()
    client, err := plinth.New(plinth.Config{
        WorkspaceURL: "http://localhost:7421",
        GatewayURL:   "http://localhost:7422",
        IdentityURL:  "http://localhost:7425", // optional
        APIKey:       "local-dev",
    })
    if err != nil {
        log.Fatal(err)
    }

    // Get-or-create a workspace.
    ws, err := client.Workspace(ctx, "research-task-1")
    if err != nil {
        log.Fatal(err)
    }

    // Versioned KV writes.
    if _, err := ws.KV.Set(ctx, "topic", "renewable energy"); err != nil {
        log.Fatal(err)
    }
    val, version, err := ws.KV.GetWithVersion(ctx, "topic")
    if err != nil {
        log.Fatal(err)
    }
    fmt.Printf("topic = %q (version %d)\n", val, version)

    // Files.
    if _, err := ws.Files.WriteText(ctx, "report.md", "# Report\n", nil); err != nil {
        log.Fatal(err)
    }

    // Snapshots + branches.
    snap, _ := ws.Snapshot(ctx, "baseline", "initial state")
    branch, _ := ws.Branch(ctx, "experiment", snap.ID)
    branched := ws.WithBranch(branch.ID)
    _, _ = branched.KV.Set(ctx, "topic", "alt-energy") // writes to branch only

    // Tools.
    result, err := client.Tools.Invoke(ctx, "web.fetch",
        map[string]any{"url": "mock://example"},
        plinth.InvokeOpts{WorkspaceID: ws.ID()},
    )
    if err != nil {
        log.Fatal(err)
    }
    fmt.Printf("fetch result: %v (cached=%v)\n", result.Result, result.Cached)
}
```

## Surface

| Feature                              | Go SDK | Python SDK | TS SDK |
|--------------------------------------|--------|------------|--------|
| Workspaces (get-or-create, list, …)  | yes    | yes        | yes    |
| KV (versioned set/get/history)       | yes    | yes        | yes    |
| Files (versioned blob storage)       | yes    | yes        | yes    |
| Snapshots + branches                 | yes    | yes        | yes    |
| Channels (typed message queues)      | yes    | yes        | yes    |
| Workflows (durable, resumable)       | yes    | yes        | yes    |
| Workflow worker leases (v0.5)        | yes    | yes        | yes    |
| Tools (`Invoke`, `DryRun`, audit)    | yes    | yes        | yes    |
| Identity (token issue/verify/revoke) | yes    | yes        | yes    |
| Signing keys (RS256, v0.4)           | yes    | yes        | yes    |
| Tenant quotas (v1.0)                 | yes    | yes        | yes    |
| Generic resource locks (v0.6)        | yes    | yes        | yes    |
| Multi-region failover (v1.0)         | no     | yes        | yes    |
| LLM facade                           | no     | yes        | yes    |
| `@agent` decorator                   | no     | yes        | no     |

## Errors

Every SDK call returns a typed `*plinth.PlinthError`. Sentinel values cover every code from `CONTRACTS.md` so `errors.Is` works naturally:

```go
_, err := client.Tools.Invoke(ctx, "web.fetch", args, plinth.InvokeOpts{})
switch {
case errors.Is(err, plinth.ErrToolNotFound):
    // tool was never registered
case errors.Is(err, plinth.ErrRateLimited):
    var pe *plinth.PlinthError
    _ = errors.As(err, &pe)
    time.Sleep(time.Duration(pe.RetryAfter * float64(time.Second)))
case err != nil:
    log.Fatal(err)
}
```

`PlinthError` carries `Code`, `Message`, `Details`, `StatusCode`, `Body`, plus `RetryAfter` / `LimitType` for 429 responses.

## Limitations in v0.1

- **No LLM layer.** The Python and TS SDKs ship a pluggable LLM facade with retries and audit. The Go SDK punts that to a future release — v0.1 focuses on the v1.0 core (workspaces + tools + identity + workers + workflows + channels + locks).
- **No streaming workflow execution.** The `WorkersClient` lets you register, heartbeat, and drain — wire your own polling loop, or wait for `plinth-workflow-worker-go` (planned).
- **No multi-region failover.** The Python and TS SDKs implement deterministic per-request fallback to alternate regions. Go v0.1 ships a single base URL per service; configure your `*http.Client.Transport` for retry-with-backoff if you need it now.
- **No async iteration helpers.** Idiomatic Go uses goroutines + channels for that — pull `Receive` results in a loop and dispatch yourself.

## Testing

The package uses Go's standard `testing` + `httptest` — no external test deps. Run from the repo root:

```bash
cd sdk/go
go test ./...
go test -race ./...
go vet ./...
```

The `plinth_test/helpers_test.go` file exposes a small `MockServer` if you want to write SDK-level integration tests in your own service.

## License

Apache-2.0. Same as every other Plynf component.
