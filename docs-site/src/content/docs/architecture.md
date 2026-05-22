---
title: Architecture
description: How the pieces fit together. A 10-minute walkthrough of Plynf's primitives and services.
section: overview
order: 2
sourceFile: ARCHITECTURE.md
---

A 10-minute read for anyone wanting to understand how Plynf fits together. For component-level depth, see `docs/architecture/` in the repo.

## The thesis

Most AI agents today are wrappers around interfaces designed for humans. The cost is high: tokens spent re-reading state from chat, latency from human-paced UI flows, errors from visual ambiguity, no way to resume after crashes.

Plynf's bet: **the next infrastructure layer is the agent-native substrate**. Treat the agent as the first-class user. Give it persistent state, semantic tools, observability, and identity — purpose-built for how agents actually reason.

## The five primitives

Plynf is built around five independent-but-composable primitives:

1. **Persistent Structured Workspace** — versioned KV + files + snapshots + branches. The agent's memory across sessions, with rollback semantics.
2. **Universal Tool Gateway** — one auth boundary for all MCP / REST / GraphQL tools, with caching, idempotency, dry-run, and audit.
3. **Coordination Primitives** — multi-agent channels, locks, durable workflows.
4. **Observability Plane** — semantic event log of every action, with cost attribution.
5. **Agent-Scoped Identity** — capability tokens for delegated AI access.

## System layout

```
                        ┌──────────────────────────┐
                        │       Your Agent         │
                        │   (Python or TS SDK)     │
                        └──┬─────────────┬─────────┘
                           │             │  Authorization: Bearer <JWT>
              ┌────────────▼─────┐ ┌─────▼─────────┐ ┌───────────────┐
              │  Workspace       │ │ Tool Gateway  │ │ Identity      │
              │  :7421           │ │ :7422         │ │ :7425         │
              │                  │ │               │ │               │
              │ • KV + Files     │ │ • MCP Proxy   │ │ • Issue JWT   │
              │ • Versioning     │ │ • Caching     │ │ • Verify      │
              │ • Snapshots      │ │ • Audit log   │ │ • Revoke      │
              │ • Branches       │ │ • Rate limits │ │ • Tenants     │
              │ • Channels       │ │ • Cost caps   │ │ • JWKS        │
              │ • Workflows      │ │ • OAuth (PKCE)│ └───────────────┘
              │ • Tenants        │ │ • Tenants     │
              └────────┬─────────┘ └───┬───────────┘
                       │               │ proxies + auth
                ┌──────▼────┐   ┌──────▼─────────┐  ┌──────────────┐
                │ SQLite +  │   │  Mock MCP :7423│  │ GitHub MCP   │
                │ blobs     │   │  6 tools       │  │ :7426        │
                └───────────┘   └────────────────┘  │ 7 GitHub tools│
                                                    └──────────────┘
```

Both core services are independent processes. They share a data dir but no in-process state. They can be deployed separately, scaled separately.

## Workspace deep-dive

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
- Reads on a branch see: branch-specific writes, then fall-through to the parent snapshot.
- Merges produce a new snapshot on the parent branch's history.
- Key use case: agent does "what-if" exploration, commits or discards atomically.

### Why this matters for agents
- Agents make probabilistic decisions. Branches give them a sandbox.
- Resumability: snapshot before risky step → restore on failure.
- Audit: every state mutation is versioned and timestamped.
- Hand-offs: agent A snapshots, agent B branches from that snapshot.

## Tool Gateway deep-dive

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
        │ Policy  │ — capability check
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
- Every invocation appends an `AuditEvent`.
- Captures: tool_id, args_hash, result_hash, workspace_id, agent_id, duration, cost estimate, error.
- Queryable via `GET /v1/audit?workspace_id=...&since=1h`.
- Append-only with cryptographic chaining.

## Coordination, observability, identity

### Coordination
- **Channels** — typed message passing between agents (durable + replayable).
- **Locks/Leases** — prevent two agents from conflicting on the same resource.
- **Workflow transactions** — group tool calls into atomic units with compensating actions.

### Observability
- Per-tool audit log in the gateway.
- Unified semantic event stream. Each event captures what happened, why, what it cost, and the resulting state diff.
- OTLP-compatible export (Datadog, Tempo, Honeycomb, any OTLP collector).

### Identity
- Capability tokens — short-lived JWTs encoding *exactly* what the agent may do.
- Issued by an Identity service. Verified at the gateway and workspace.
- Capability spec: `{agent_id, workspace_id, tool_scopes: [...], expires_at}`.
- RS256 with automatic key rotation, JWKS publishing, federated revocation.

## Why these pieces, in this order?

The minimum viable agent substrate needs:
1. A place to put state (Workspace) — without it, agents are stateless and brittle.
2. A way to call tools coherently (Gateway) — without it, integration sprawl kills you.

Coordination, observability, and identity are *extensions* of those two. Build the substrate first; the rest snaps in.
