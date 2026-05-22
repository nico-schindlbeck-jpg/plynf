# `@plinth/sdk` — Plynf TypeScript SDK

The official TypeScript / JavaScript client for [Plynf](../../README.md),
the agent-native runtime layer.

> **TL;DR:** A versioned workspace + tool gateway + identity service,
> wrapped in an ergonomic TypeScript client so your agent's state, tool
> calls, and capability tokens are persistent, auditable, and cheap to
> replay.

```bash
npm install @plinth/sdk
```

```ts
import { Plynf } from "@plinth/sdk";

const client = new Plynf({ apiKey: "local-dev" });
const ws = await client.workspace("research-task-1");
await ws.kv.set("topic", "renewable energy");
const result = await client.tools.invoke("web.fetch", { url: "mock://ipcc-2023" });
await ws.snapshot("baseline");
```

That's it. Five lines, persistent state, audited tools.

---

## Table of contents

- [Installation](#installation)
- [Quickstart](#quickstart)
- [Authentication](#authentication)
- [Workspaces](#workspaces)
  - [Versioned KV](#versioned-kv)
  - [Versioned files](#versioned-files)
  - [Snapshots](#snapshots)
  - [Branches](#branches)
  - [Channels (v0.2)](#channels-v02)
  - [Workflows (v0.2)](#workflows-v02)
- [Tools](#tools)
  - [Invoke](#invoke)
  - [Dry-run](#dry-run)
  - [Register](#register)
  - [Audit](#audit)
- [Identity (v0.3)](#identity-v03)
- [Token counting](#token-counting)
- [Error handling](#error-handling)
- [Configuration reference](#configuration-reference)
- [Parity with the Python SDK](#parity-with-the-python-sdk)
- [Development](#development)

---

## Installation

```bash
npm install @plinth/sdk
# or
pnpm add @plinth/sdk
# or
yarn add @plinth/sdk
```

Requires **Node 20+** (uses the global `fetch`). ESM only — there is no
CommonJS build. Strict-mode TypeScript is fully supported.

The only runtime dependency is
[`gpt-tokenizer`](https://www.npmjs.com/package/gpt-tokenizer) (~150 KB)
for offline `cl100k_base` token counting. If `gpt-tokenizer` is
unavailable at runtime, `countTokens` falls back to a `words × 1.3`
heuristic and logs a one-shot warning — see
[Token counting](#token-counting).

## Quickstart

Spin up the services (see the project [README](../../README.md) for
`make serve`), then:

```ts
import { Plynf } from "@plinth/sdk";

const client = new Plynf({
  workspaceUrl: "http://localhost:7421",
  gatewayUrl:   "http://localhost:7422",
  identityUrl:  "http://localhost:7425",   // optional, v0.3
  apiKey:       "local-dev",                // any non-empty string in dev
  timeoutMs:    30_000,
});

// Get-or-create a workspace by name.
const ws = await client.workspace("research-task-1");

// Versioned KV.
await ws.kv.set("topic", "renewable energy");
console.log(await ws.kv.get("topic"));     // → "renewable energy"

// Versioned files.
await ws.files.write("report.md", "# Report\n...");
console.log(await ws.files.readText("report.md"));

// Snapshot the state.
const snap = await ws.snapshot("baseline", { message: "initial state" });

// Tool calls (audited, cached).
const result = await client.tools.invoke("web.fetch", { url: "mock://ipcc-2023" });
console.log(result.cached, result.duration_ms);
```

## Authentication

In v0.3 Plynf ships an identity service that issues JWT capability
tokens. The TypeScript SDK accepts the encoded token as `apiKey`:

```ts
const bootstrap = new Plynf({
  identityUrl: "http://localhost:7425",
  apiKey: "bootstrap-secret",
});

const issued = await bootstrap.identity.issueToken({
  agentId: "researcher",
  scopes: ["tool:web.fetch:read", "workspace:my-task:write"],
  ttlSeconds: 3_600,
});

// Use the scoped token for the actual agent client.
const agent = new Plynf({
  workspaceUrl: "http://localhost:7421",
  gatewayUrl:   "http://localhost:7422",
  apiKey: issued.token,
});
```

Tokens carry `scopes` like `tool:<tool_id>:read`,
`workspace:<id>:write`, and `tenant:<id>:admin`. See
[CONTRACTS.md](../../CONTRACTS.md#scope-grammar) for the full grammar.

## Workspaces

A *workspace* is the top-level isolation boundary for one agent's
state. `client.workspace(name)` returns a handle, creating the workspace
if it does not yet exist:

```ts
const ws = await client.workspace("research-task-1");
ws.id;        // → "ws_01H..."
ws.name;      // → "research-task-1"
```

If you already have a workspace ID:

```ts
const ws = await client.getWorkspace("ws_01H...");
```

### Versioned KV

Every write creates a new immutable version. Reads return the latest by
default; pass `{ version: N }` to read a specific revision.

```ts
// Write — returns a KVEntry.
const entry = await ws.kv.set("topic", "renewable energy");
entry.version;                              // → 1

// Read — value only, or null for tombstones / missing keys.
await ws.kv.get("topic");                   // → "renewable energy"

// Read — full KVEntry with version metadata.
const meta = await ws.kv.getWithMeta("topic");
const v1   = await ws.kv.getWithMeta("topic", { version: 1 });

// History — every version, oldest first.
for (const h of await ws.kv.history("topic")) {
  console.log(h.version, h.value);
}

// List the latest version of every key.
for (const e of await ws.kv.list()) {
  console.log(e.key);
}

// Delete (creates a tombstone version).
await ws.kv.delete("topic");
```

### Versioned files

Same model as KV, but for byte-addressable content with paths:

```ts
// Strings are auto-encoded UTF-8.
await ws.files.write("report.md", "# Report\n...");

// Bytes — pass content_type for accurate metadata.
await ws.files.write(
  "data.bin",
  new Uint8Array([0, 1, 2]),
  { contentType: "application/octet-stream" },
);

// Read raw bytes…
const content: Uint8Array = await ws.files.read("report.md");

// …or text (UTF-8 by default).
const text: string = await ws.files.readText("report.md");

// Metadata only (no body download).
const meta = await ws.files.meta("report.md");
console.log(meta.size, meta.sha256, meta.content_type);

await ws.files.delete("old.md");
const all = await ws.files.list();
```

### Snapshots

A snapshot freezes the latest version of every key and file in the
workspace. Snapshots are immutable and cheap (they reference versions,
not data).

```ts
const snap = await ws.snapshot("baseline", { message: "initial state" });

const all = await ws.listSnapshots();
const one = await ws.getSnapshot(snap.id);
const diff = await ws.diff(snap.id, otherSnap.id);
console.log(diff.kv_added, diff.files_modified);
```

### Branches

A branch is a divergent timeline forked from a snapshot. Writes against
a branch don't affect main until merged.

```ts
const branch = await ws.branch("experiment", { fromSnapshot: snap.id });

// Get a Workspace view scoped to the branch — every KV/file/snapshot
// call automatically targets this branch.
const wsBranch = ws.withBranch(branch.id);
await wsBranch.kv.set("topic", "alternative");

await ws.kv.get("topic");     // unchanged

const result = await ws.merge(branch.id);
console.log(result.merged_at, result.conflicts);

await ws.deleteBranch(branch.id);
```

### Channels (v0.2)

A *channel* is a workspace-scoped, persistent message queue with
monotonic per-channel sequence numbers. Use them for hand-offs between
agents (research → writer → reviewer pipelines, etc.).

```ts
// Send a message — the channel is created on demand.
const msg = await ws.channels.send(
  "research-out",
  { sources: [/* … */], facts: {/* … */} },
  {
    sender: "researcher",
    type:   "research.complete",
    correlationId: undefined,
    headers: {},
  },
);
console.log(msg.id, msg.seq, msg.sent_at);

// Receive messages. Pass `consumer` to track a per-consumer cursor on the
// server, so subsequent calls without `since` resume where you left off.
const msgs = await ws.channels.receive("research-out", {
  consumer: "writer",
  limit:    100,
  peek:     false,
});
for (const m of msgs) {
  process(m.payload);
  await ws.channels.ack(m);                 // delete + advance cursor
}

// Block-wait for one message — polls under the hood.
const m = await ws.channels.wait("research-out", {
  consumer:        "writer",
  timeoutMs:       30_000,
  pollIntervalMs:  500,
});
// Returns the ChannelMessage, or null on timeout.

// Channel management.
for (const ch of await ws.channels.list()) {
  console.log(ch.name, ch.message_count);
}
const ch = await ws.channels.get("research-out");
await ws.channels.deleteChannel("research-out");
```

`ws.channels.ack` accepts a full `ChannelMessage` (not a bare ID) — the
channel name is required to build the DELETE URL. Receive the message,
then ack the object you got back.

### Workflows (v0.2)

A *workflow* is a named manifest of expected step names plus a
server-tracked log of completed steps. Each step has a lifecycle of
`pending → running → (completed | failed | cancelled)` and may
reference a workspace snapshot at completion time so a crashed agent
can resume from a known checkpoint.

```ts
// Create a workflow with a step manifest.
const wf = await ws.workflows.create("research-pipeline", {
  steps: ["search", "fetch", "extract", "synthesize"],
  metadata: { topic: "renewable energy" },
});
// → WorkflowHandle, with `.id`, `.name`, `.status`, `.steps`, `.stepsManifest`.

// Idempotent: get the existing one if it's already there.
const wf2 = await ws.workflows.getOrCreate("research-pipeline", {
  steps: ["search", "fetch", "extract", "synthesize"],
});

// Attach to an existing workflow by ID.
const wf3 = await ws.workflows.get(wf.id);

// Per-step lifecycle.
const step = await wf.startStep("search", { input: { topic: "renewable energy" } });
// … work happens, the workspace mutates …
const snap = await ws.snapshot("after-search");
await wf.completeStep(step.id, { output: { sources: [/* … */] }, snapshotId: snap.id });

// Errors / cancellation.
await wf.failStep(step.id, "connection refused");
await wf.cancelStep(step.id);
await wf.cancel();                          // cancel the entire workflow

// Resume after crash — pick up at the next pending step from the last
// completed snapshot.
const resume = await wf.resumeInfo();
if (resume.next_step) {
  // Restore the workspace from `resume.snapshot_id`, then continue at
  // `resume.next_step`.
}

// Re-fetch the workflow (and its full step log) from the server.
await wf.refresh();
console.log(wf.status, wf.steps);

// List every workflow on the workspace.
for (const w of await ws.workflows.list()) {
  console.log(w.id, w.name, w.status);
}
```

`WorkflowHandle.startStep` rejects step names that are not in the
workflow's manifest (raising `InvalidWorkflowStepError` *before* the
request leaves the box). The same exception is mapped from the
server-side `INVALID_WORKFLOW_STEP` error code.

## Tools

`client.tools` is the gateway client. Tool invocations go through the
gateway, which adds caching, idempotency, and a complete audit trail.

### Invoke

```ts
const r = await client.tools.invoke("web.fetch", { url: "mock://ipcc-2023" });
r.result;            // tool-specific payload (`unknown`)
r.cached;            // boolean — was this a cache hit?
r.duration_ms;
r.cost_estimate_usd;

// Disable cache for a single call.
await client.tools.invoke("web.search", { query: "x" }, { cache: false });

// Attribute the call to a workspace + agent for audit purposes.
await client.tools.invoke(
  "web.fetch",
  { url: "..." },
  { workspaceId: ws.id, agentId: "agent-007", idempotencyKey: "dedup-123" },
);
```

### Dry-run

Predict whether the call would hit cache and what it would cost,
*without* actually invoking the tool:

```ts
const plan = await client.tools.dryRun("web.fetch", { url: "..." });
plan.would_invoke;
plan.cached_result;
plan.estimated_cost_usd;
```

### Register

```ts
await client.tools.register({
  tool_id:       "my.tool",
  name:          "My Tool",
  description:   "Action-oriented description.",
  transport:     "http",
  endpoint:      "http://my-tool.local/invoke",
  input_schema:  { type: "object", properties: { q: { type: "string" } } },
  output_schema: { type: "object" },
  idempotent:    true,
  side_effects:  "read",
  cache_ttl_seconds: 600,
});

// List, fetch, deregister.
for (const t of await client.tools.list()) console.log(t.tool_id);
const tool = await client.tools.get("web.fetch");
await client.tools.deregister("my.tool");
```

### Audit

```ts
const events = await client.tools.audit({
  workspaceId: ws.id,
  since:       "1h",          // or "30m", "7d", or ISO-8601
  limit:       100,
});
```

## Identity (v0.3)

Capability tokens live on a separate identity service (`port 7425`).
Configure it via `identityUrl`:

```ts
const client = new Plynf({
  identityUrl: "http://localhost:7425",
  apiKey:      "bootstrap-token",
});

// Mint a scoped token.
const issued = await client.identity.issueToken({
  agentId:    "researcher",
  scopes:     ["tool:web.fetch:read", "workspace:my-task:write"],
  ttlSeconds: 3_600,
});
issued.token;        // JWT
issued.jti;          // Token ID for revocation
issued.expires_at;
issued.claims;       // Decoded claims

// Verify (returns claims; throws on failure).
const claims = await client.identity.verifyToken(issued.token);

// Revoke by jti.
await client.identity.revokeToken(issued.jti);

// Inspect token metadata.
const info = await client.identity.getTokenInfo(issued.jti);
info.revoked;        // boolean

// Public keys for offline verification.
const jwks = await client.identity.getJwks();
```

Verification failures map to specific subclasses of `UnauthorizedError`:

| Code            | Class                |
| --------------- | -------------------- |
| `INVALID_TOKEN` | `InvalidTokenError`  |
| `TOKEN_EXPIRED` | `TokenExpiredError`  |
| `TOKEN_REVOKED` | `TokenRevokedError`  |

```ts
import { TokenExpiredError } from "@plinth/sdk";

try {
  await client.identity.verifyToken(suspect);
} catch (err) {
  if (err instanceof TokenExpiredError) {
    // refresh and retry…
  }
}
```

## Token counting

Plynf ships with offline token counting via the `cl100k_base` BPE
encoding (the closest publicly available BPE to Anthropic's tokenizer).
Backed by [`gpt-tokenizer`](https://www.npmjs.com/package/gpt-tokenizer).

```ts
const n = await client.countTokens("Hello world");          // → small int

// Cost estimation at Sonnet pricing ($3/M input, $15/M output).
client.estimateCost(1000, 500);                             // → USD
```

You can also import the helpers directly:

```ts
import { countTokens, estimateCost, heuristicCount } from "@plinth/sdk";

await countTokens("...");
estimateCost(1_000_000, 500_000);

// Synchronous, dependency-free fallback (`words × 1.3`).
heuristicCount("...");
```

> **Heuristic fallback.** If `gpt-tokenizer` is missing at runtime
> (e.g. the dep was tree-shaken or pruned), `countTokens` falls back to
> a `words × 1.3` heuristic and logs a one-shot warning. The heuristic
> is intentionally coarse — budget callers should treat it as
> approximate. Install `gpt-tokenizer` (or pin it as a dep) for exact
> counts.

To update pricing, edit `SONNET_INPUT_USD_PER_MTOK` /
`SONNET_OUTPUT_USD_PER_MTOK` — they are exposed as named exports for
exactly this reason.

## Error handling

Every HTTP error from the workspace, gateway, or identity services is
mapped to a typed exception. Each one carries `code`, `status`, `details`,
and the original message.

```ts
import {
  PlinthError,                // base class
  InvalidArgumentsError,      // 400
  InvalidWorkflowStepError,   // 400 (subclass of InvalidArgumentsError)
  UnauthorizedError,          // 401
  InvalidTokenError,          // 401 (subclass of UnauthorizedError)
  TokenExpiredError,          // 401 (subclass of UnauthorizedError)
  TokenRevokedError,          // 401 (subclass of UnauthorizedError)
  WorkspaceNotFoundError,     // 404
  KeyNotFoundError,           // 404
  FileNotFoundError,          // 404
  SnapshotNotFoundError,      // 404
  BranchNotFoundError,        // 404
  ToolNotFoundError,          // 404
  ChannelNotFoundError,       // 404 (v0.2)
  MessageNotFoundError,       // 404 (v0.2)
  WorkflowNotFoundError,      // 404 (v0.2)
  WorkflowStepNotFoundError,  // 404 (v0.2)
  RateLimitedError,           // 429
  CostCapExceededError,       // 429 (subclass of RateLimitedError)
  ToolInvocationError,        // tool was found but failed
} from "@plinth/sdk";

try {
  await ws.kv.getWithMeta("missing");
} catch (err) {
  if (err instanceof KeyNotFoundError) {
    // err.code     → "KEY_NOT_FOUND"
    // err.status   → 404
    // err.details  → service-specific payload
  } else {
    throw err;
  }
}
```

| Class                       | HTTP | Code                     |
| --------------------------- | ---- | ------------------------ |
| `WorkspaceNotFoundError`    | 404  | `WORKSPACE_NOT_FOUND`    |
| `KeyNotFoundError`          | 404  | `KEY_NOT_FOUND`          |
| `FileNotFoundError`         | 404  | `FILE_NOT_FOUND`         |
| `SnapshotNotFoundError`     | 404  | `SNAPSHOT_NOT_FOUND`     |
| `BranchNotFoundError`       | 404  | `BRANCH_NOT_FOUND`       |
| `ToolNotFoundError`         | 404  | `TOOL_NOT_FOUND`         |
| `ChannelNotFoundError`      | 404  | `CHANNEL_NOT_FOUND`      |
| `MessageNotFoundError`      | 404  | `MESSAGE_NOT_FOUND`      |
| `WorkflowNotFoundError`     | 404  | `WORKFLOW_NOT_FOUND`     |
| `WorkflowStepNotFoundError` | 404  | `WORKFLOW_STEP_NOT_FOUND`|
| `InvalidArgumentsError`     | 400  | `INVALID_ARGUMENTS`      |
| `InvalidWorkflowStepError`  | 400  | `INVALID_WORKFLOW_STEP`  |
| `UnauthorizedError`         | 401  | `UNAUTHORIZED`           |
| `InvalidTokenError`         | 401  | `INVALID_TOKEN`          |
| `TokenExpiredError`         | 401  | `TOKEN_EXPIRED`          |
| `TokenRevokedError`         | 401  | `TOKEN_REVOKED`          |
| `RateLimitedError`          | 429  | `RATE_LIMITED`           |
| `CostCapExceededError`      | 429  | `COST_CAP_EXCEEDED`      |
| `ToolInvocationError`       | —    | `TOOL_INVOCATION_FAILED` |
| `PlinthError` (fallback)    | any  | other                    |

## Configuration reference

```ts
new Plynf({
  workspaceUrl: "http://localhost:7421",
  gatewayUrl:   "http://localhost:7422",
  identityUrl:  "http://localhost:7425",   // optional, v0.3
  apiKey:       "local-dev",
  timeoutMs:    30_000,
  fetch:        globalThis.fetch,           // for testing/edge runtimes
});
```

| Field          | Type           | Required | Default                  |
| -------------- | -------------- | :------: | ------------------------ |
| `workspaceUrl` | `string`       |   no     | `http://localhost:7421`  |
| `gatewayUrl`   | `string`       |   no     | `http://localhost:7422`  |
| `identityUrl`  | `string`       |   no     | unset (identity disabled)|
| `apiKey`       | `string`       |   yes    | —                        |
| `timeoutMs`    | `number`       |   no     | `30_000`                 |
| `fetch`        | `typeof fetch` |   no     | `globalThis.fetch`        |

## Parity with the Python SDK

| Capability                                  | Python | TypeScript |
| ------------------------------------------- | :----: | :--------: |
| Get-or-create workspace by name             |   ✅   |     ✅     |
| KV set / get / history / delete / list      |   ✅   |     ✅     |
| Files write / read / meta / delete / list   |   ✅   |     ✅     |
| Snapshots: create, list, diff               |   ✅   |     ✅     |
| Branches: create, withBranch, merge, delete |   ✅   |     ✅     |
| Tool invoke / dryRun / register / audit     |   ✅   |     ✅     |
| Channels: send / receive / wait / ack       |   ✅   |     ✅     |
| Workflows: create / steps / resumeInfo      |   ✅   |     ✅     |
| Identity: issue / verify / revoke           |   ✅   |     ✅     |
| Token counting (`cl100k_base`)              |   ✅   |     ✅     |
| Sonnet cost estimation                      |   ✅   |     ✅     |
| Typed error hierarchy                       |   ✅   |     ✅     |
| `?branch=` propagation                      |   ✅   |     ✅     |
| Bearer auth on every request                |   ✅   |     ✅     |
| `@agent` decorator                          |   ✅   | `withAgent`|

The TypeScript SDK exposes `client.withAgent(name, workspaceName, fn)`
in place of the Python `@client.agent` decorator — TypeScript decorators
don't translate cleanly to runtime callbacks, and the explicit form
makes type inference work out of the box.

## Development

```bash
cd sdk/typescript
npm install
npm run build       # tsc → dist/
npm test            # vitest (79 tests)
```

The SDK has one runtime dependency (`gpt-tokenizer`). Test-time
dependencies are `vitest` and `@types/node`.

## License

Apache-2.0 © The Plynf Authors
