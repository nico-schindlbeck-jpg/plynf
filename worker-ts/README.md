# @plinth/workflow-worker

Durable workflow worker for Node.js — TypeScript counterpart of the Python
[`plinth-workflow-worker`](../worker/README.md).

The worker process polls the Plinth workspace service for pending workflow
steps, acquires a lease, dispatches to a registered handler, then releases
the lease. On crash, the workspace's lease reaper expires the worker's
leases and reverts the steps back to `pending` so another worker can take
over.

## Install

```bash
npm install @plinth/workflow-worker @plinth/sdk
```

(Inside this monorepo, `@plinth/sdk` resolves via a local `file:` link in
`package.json`.)

## Quickstart — embedded worker

```ts
import { Plinth } from "@plinth/sdk";
import { Worker, WorkflowRuntime } from "@plinth/workflow-worker";

const client = new Plinth({
  workspaceUrl: "http://localhost:7421",
  gatewayUrl: "http://localhost:7422",
  apiKey: "local-dev",
});

const runtime = new WorkflowRuntime();

runtime.register("research-pipeline", "search", async (ctx) => {
  const { topic } = ctx.step.input as { topic: string };
  const result = await ctx.tools.invoke("web.search", { query: topic, k: 5 });
  return { sources: result.result };
});

const worker = new Worker({ client, runtime, concurrency: 2 });
process.once("SIGTERM", () => worker.stop());
process.once("SIGINT",  () => worker.stop());
await worker.run();          // blocks until stop() is called
```

## Quickstart — CLI

Compile a small handlers module that exports `register(runtime, client)`:

```ts
// handlers.ts
import type { Plinth } from "@plinth/sdk";
import type { WorkflowRuntime } from "@plinth/workflow-worker";

export function register(runtime: WorkflowRuntime, client: Plinth): void {
  runtime.register("research-pipeline", "search", async (ctx) => {
    const { topic } = ctx.step.input as { topic: string };
    return { topic, when: new Date().toISOString() };
  });
}
```

Then run the CLI:

```bash
plinth-workflow-worker \
  --workspace-url http://localhost:7421 \
  --gateway-url http://localhost:7422 \
  --api-key local-dev \
  --concurrency 4 \
  --lease-ttl 60 \
  --heartbeat-interval 15 \
  --handlers-module ./handlers.js
```

`--handlers-module` accepts a relative or absolute file path
(`./handlers.js`, `/srv/app/handlers.js`) or a bare package specifier
(`my-app/handlers`). The worker imports it and calls
`register(runtime, client)` to populate the dispatch table before it
starts polling.

## Handler registration patterns

```ts
// 1. Imperative
runtime.register("wf", "step", async (ctx) => { /* ... */ });

// 2. handler() helper — keeps the function reference for later use
const search = runtime.handler("research-pipeline", "search")(async (ctx) => {
  /* ... */
});
```

Each `(workflow, step)` key may be registered exactly once. Re-registering
throws so deployment-time typos surface immediately.

## CLI flags

All flags can also be set via environment variables (`PLINTH_*`). CLI
overrides env, env overrides defaults.

| Flag | Env var | Default | Notes |
|------|---------|---------|-------|
| `--workspace-url` | `PLINTH_WORKSPACE_URL` | `http://localhost:7421` | |
| `--gateway-url` | `PLINTH_GATEWAY_URL` | `http://localhost:7422` | |
| `--identity-url` | `PLINTH_IDENTITY_URL` | (unset) | Optional. |
| `--api-key` | `PLINTH_API_KEY` | `local-dev` | Bearer token. |
| `--concurrency` | `PLINTH_CONCURRENCY` | `4` | In-flight steps. |
| `--lease-ttl` | `PLINTH_LEASE_TTL` | `60` | Seconds. |
| `--heartbeat-interval` | `PLINTH_HEARTBEAT_INTERVAL` | `15` | Seconds. |
| `--worker-heartbeat-interval` | `PLINTH_WORKER_HEARTBEAT_INTERVAL` | `30` | Seconds. |
| `--poll-interval` | `PLINTH_POLL_INTERVAL` | `2` | Seconds. |
| `--handlers-module` | `PLINTH_HANDLERS_MODULE` | — | Required. |
| `--workspace` | — | (all) | Whitelist (can repeat). |
| `--silent` | — | off | Suppress info/warn logs. |
| `--version` | — | — | Print version and exit. |

## Crash recovery

Like the Python worker, this binary is durable in two ways:

1. **Worker-level**: A worker registers + heartbeats on a 30s cadence by
   default. If the worker crashes (no heartbeat for the workspace's
   `worker_inactive_timeout_seconds`), the workspace marks it `gone`.
2. **Lease-level**: While running a step, the worker heartbeats the
   lease every `heartbeat-interval` seconds. If the worker dies
   mid-step, the reaper sweeps the expired lease and reverts the step
   from `running` back to `pending`. Another worker (or this one after
   restart) sees it as a candidate and takes over.

Workspace state — KV writes, file writes, snapshots — is preserved by
the workspace service, so the next worker resumes from the most recent
committed state. Take snapshots at step boundaries to make resume
points explicit.

## Parity with the Python worker

| Capability | Python | TypeScript |
|-----------:|:------:|:----------:|
| Polling pending steps | yes | yes |
| Race-safe leasing | yes | yes |
| Per-lease heartbeat | yes | yes |
| Worker-level heartbeat | yes | yes |
| Graceful drain on SIGTERM/SIGINT | yes | yes |
| Workspace whitelist (`--workspace`) | yes | yes |
| `--handlers-module` import | yes | yes |
| Async + sync handlers | yes | yes |
| Configurable concurrency | yes | yes |
| Validates `heartbeat < lease_ttl` | yes | yes |

### Surface differences

- **Handler registration**. The Python worker uses
  `@client.workflow_handler(...)` decorators that fire at import time.
  The Node worker uses an explicit `register(runtime, client)` export
  because ESM doesn't make import-time side effects reliable across
  bundlers and runtimes (verbatim modules, tree-shaking, etc.).
- **Logging**. The Python worker uses structlog by default. The Node
  worker emits one-line JSON to stderr; pass `logger: { info, warn, debug }`
  to swap in pino, winston, etc., or `logger: null` for silent.
- **Ergonomics**. `runtime.handler("wf", "step")(fn)` is a small helper
  for registration that doesn't need decorator syntax.

## Embedded worker — programmatic API

```ts
import { Plinth } from "@plinth/sdk";
import { Worker, WorkflowRuntime, type WorkerLogger } from "@plinth/workflow-worker";

const logger: WorkerLogger = {
  info: (event, fields) => console.log("INFO", event, fields),
  warn: (event, fields) => console.warn("WARN", event, fields),
  debug: () => {},
};

const worker = new Worker({
  client,
  runtime,
  concurrency: 2,
  leaseTtlSeconds: 60,
  heartbeatIntervalSeconds: 15,
  pollIntervalSeconds: 2,
  workspaceFilter: ["my-app-prod"],   // optional
  logger,
});

await worker.run();
```

`Worker.run()` resolves once `worker.stop()` has been called and every
slot has drained — that's your cue to exit the process.

## Limitations

- **No streaming output**. Like the Python worker, the TS worker passes
  the handler's full return value through to `release(output=...)`.
  Long-running steps that want progress visibility should write KV /
  channels themselves.
- **No retries inside the worker**. If a handler throws, the step is
  marked `failed`. To retry, your driver re-creates a new pending step
  with the same name. The Python worker has the same behaviour.
- **`@plinth/sdk` is a peer-ish dependency**. The package depends on a
  specific SDK version (currently `file:../sdk/typescript` inside this
  monorepo). When publishing, switch to a pinned semver range.

## License

Apache-2.0
