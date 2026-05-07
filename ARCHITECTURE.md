# Plinth — Architecture Overview

> A 10-minute read for anyone wanting to understand how Plinth fits together. For component-level depth, see `docs/architecture/`. For decisions and trade-offs, see `docs/adr/`.

## 1. The thesis

Most AI agents today are wrappers around interfaces designed for humans. The cost is high: tokens spent re-reading state from chat, latency from human-paced UI flows, errors from visual ambiguity, no way to resume after crashes.

Plinth's bet: **the next infrastructure layer is the agent-native substrate**. Treat the agent as the first-class user. Give it persistent state, semantic tools, observability, and identity — purpose-built for how agents actually reason.

## 2. The five primitives

Plinth is built around five independent-but-composable primitives:

1. **Persistent Structured Workspace** — versioned KV + files + snapshots + branches. The agent's memory across sessions, with rollback semantics.
2. **Universal Tool Gateway** — one auth boundary for all MCP / REST / GraphQL tools, with caching, idempotency, dry-run, and audit.
3. **Coordination Primitives** — multi-agent channels, locks, durable workflows. *(v0.2)*
4. **Observability Plane** — semantic event log of every action, with cost attribution. *(partially in v0.1 via gateway audit)*
5. **Agent-Scoped Identity** — capability tokens for delegated AI access. *(v0.2)*

v0.1 ships **(1) and (2)** as a working PoC. **(3)–(5)** are designed in `docs/architecture/` and stubbed in code.

## 3. System layout

```
┌──────────────────────────────────────────────────────────────────┐
│                          AGENT                                   │
│                  (Python or TypeScript SDK)                      │
└───────────────────────┬──────────────────────────────────────────┘
                        │
              ┌─────────┴──────────┐
              │                    │
        Workspace API         Tool Gateway
        :7421                 :7422
        ┌──────────────┐     ┌──────────────────┐
        │ FastAPI      │     │ FastAPI          │
        │              │     │                  │
        │ • KV         │     │ • Tool registry  │
        │ • Files      │     │ • Invoke proxy   │
        │ • Snapshots  │     │ • Caching layer  │
        │ • Branches   │     │ • Audit log      │
        │ • Versioning │     │ • Auth manager   │
        │ • Diff/Merge │     │ • Dry-run mode   │
        └──────┬───────┘     └─────────┬────────┘
               │                       │
        ┌──────▼───────┐         ┌─────▼──────────┐
        │ SQLite       │         │ MCP servers    │
        │ + blobs/     │         │ (real or mock) │
        └──────────────┘         └────────────────┘
```

Both services are independent processes. They share a data dir but no in-process state. They can be deployed separately, scaled separately.

## 4. Workspace deep-dive

### Data model
```
Workspace (1)
  ├─ KVEntries  (many, versioned per key)
  ├─ FileEntries (many, versioned per path)
  ├─ Snapshots  (many, immutable, capture { kv → version, file → version })
  └─ Branches   (many, point to a snapshot, allow divergent writes)
```

### Versioning semantics
- Every PUT to KV/files creates a new immutable version (monotonic int per key).
- Latest version is the default read; specific version with `?version=N`.
- Deletes create a tombstone version (same number sequence).

### Snapshots
- A snapshot is a *named* point-in-time view over the workspace.
- Captures the current latest version of every key and file.
- Cheap: snapshots are metadata only (refs to existing versions).

### Branches
- A branch is a writable workspace fork from a snapshot.
- Reads on a branch see: branch-specific writes ▸ fall-through to from_snapshot.
- Merges produce a new snapshot on the parent branch's history.
- Key use case: agent does "what-if" exploration, commits or discards atomically.

### Why this matters for agents
- Agents make probabilistic decisions. Branches give them a sandbox.
- Resumability: snapshot before risky step → restore on failure.
- Audit: every state mutation is versioned & timestamped.
- Hand-offs: agent A snapshots, agent B branches from that snapshot.

## 5. Tool Gateway deep-dive

### Why a gateway, not direct MCP calls?

If your agent calls 10 MCP servers directly, you have:
- 10 OAuth flows to manage
- 10 different error conventions
- 10 unbounded rate limits
- No unified audit trail
- No caching (every duplicate call costs full tokens + provider $$)

The gateway centralises all of these.

### Request flow

```
Agent → POST /v1/invoke {tool_id, args, workspace_id?}
            │
            ▼
        ┌─────────┐
        │ Policy  │ — capability check (post-v0.1)
        └────┬────┘
             │
        ┌────▼────┐
        │ Cache   │ — SHA256(tool_id + args), TTL per tool
        └────┬────┘    Hit: return cached result, mark cached=true
             │
        ┌────▼────┐
        │ Auth    │ — fetch credentials for this tool
        └────┬────┘
             │
        ┌────▼─────┐
        │ Proxy    │ — call MCP/HTTP backend
        └────┬─────┘
             │
        ┌────▼────┐
        │ Audit   │ — log tool_id, args_hash, result_hash, duration, cost
        └────┬────┘
             │
        ┌────▼────┐
        │ Cache   │ — store result for next time (if idempotent)
        └────┬────┘
             │
             ▼
        Return InvokeResponse
```

### Caching rules
- Each tool declares `cache_ttl_seconds` and `idempotent` at registration.
- `idempotent=false` → never cached.
- `idempotent=true` → cached for `cache_ttl_seconds`.
- Cache key = `sha256(tool_id || canonical_json(args))`.
- Disabled per-call with `cache=false` in InvokeRequest.

### Audit log
- Every invocation appends an `AuditEvent` to SQLite.
- Captures: tool_id, args_hash, result_hash, workspace_id, agent_id, duration, cost estimate, error.
- Queryable via `GET /v1/audit?workspace_id=...&since=1h`.
- Append-only (no updates/deletes in PoC; cryptographic chaining post-v0.1).

## 6. Coordination, observability, identity (v0.2 sketches)

### Coordination
- **Channels** — typed message passing between agents (ZMQ-style, but persistent and replayable).
- **Locks/Leases** — prevent two agents from conflicting on the same resource.
- **Workflow transactions** — group tool calls into atomic units with compensating actions.

The right substrate is probably Temporal underneath, with Plinth-native primitives layered on top. ADR 0004 covers it.

### Observability
- v0.1: per-tool audit log in the gateway.
- v0.2: unified semantic event stream. Each event captures what happened, why (reasoning trace), what it cost, and the resulting state diff.
- Standards: OTLP-compatible export.

### Identity
- v0.1: bearer token (any non-empty string accepted in PoC).
- v0.2: capability tokens — short-lived JWTs encoding *exactly* what the agent may do. Issued by an Identity service. Verified at the gateway.
- Capability spec: `{agent_id, workspace_id, tool_scopes: [...], expires_at}`.

## 7. Why these pieces, in this order?

The minimum viable agent substrate needs:
1. A place to put state (Workspace) — without it, agents are stateless and brittle.
2. A way to call tools coherently (Gateway) — without it, integration sprawl kills you.

Coordination, observability, and identity are *extensions* of those two. Build the substrate first; the rest snaps in.

## 8. Non-goals (v0.1)

- ❌ Distributed/clustered runtime — single-node only
- ❌ Real OAuth — mock auth in the PoC
- ❌ Multi-tenant isolation — one workspace cluster, one user
- ❌ Real production observability (Prometheus, OTLP) — structured logs only
- ❌ Authn for the workspace API itself beyond a static token

These all belong post-v0.1, prioritised by real customer pull.

## 9. Reading order

1. This file (you're here).
2. [`docs/why-plinth.md`](./docs/why-plinth.md) — the strategic context.
3. [`CONTRACTS.md`](./CONTRACTS.md) — exact API surface.
4. [`docs/architecture/02-workspace-design.md`](./docs/architecture/02-workspace-design.md) — workspace internals.
5. [`docs/architecture/03-tool-gateway-design.md`](./docs/architecture/03-tool-gateway-design.md) — gateway internals.
6. [`docs/adr/`](./docs/adr/) — every important "why" decision.
7. [`examples/01-research-agent/`](./examples/01-research-agent/) — see it work.
