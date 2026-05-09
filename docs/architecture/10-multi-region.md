# 10 — Multi-Region Architecture (v1.0 Scaffolding)

> Companion to `docs/multi-region.md` (operator playbook). This doc
> dives into the per-service replication strategy and the moving parts
> behind the v1.0 scaffolding.

## Design principles

Three commitments shape the v1.0 multi-region surface:

1. **Opt-in by default** — `standalone` mode is the default and is
   bit-for-bit identical to the v0.6 deployment. Operators see no
   behaviour change unless they flip `PLINTH_REPLICATION_MODE`.
2. **Operator-side orchestration** — the actual cross-region pull /
   replay is a cron job, sidecar, or agent. Plinth provides the API
   surface; you wire the plumbing. This keeps the platform out of the
   way of operators who already have replication infrastructure.
3. **Idempotent everywhere** — the apply endpoint dedupes on `seq`,
   the SDK fallback queue dedupes on URL, and replicas redirect rather
   than reject silently. Retries are always safe.

## Per-service strategy

### Workspace — log-shipping (SQLite) or streaming (Postgres)

Workspace is the only stateful service that *needs* a replication
primitive in v1.0 — it owns the kv / file / channel / workflow tables
that diverge between regions. SQLite deployments use the
`replication_log` table; Postgres deployments are expected to use
streaming replication via a managed service (RDS / Cloud SQL / Aurora
Global).

The log captures every successful mutating verb in primary mode:

```
seq | kind                   | workspace_id | payload                           | region_id
----+------------------------+--------------+-----------------------------------+-----------
1   | workspace.post         | NULL         | {"method":"POST","path":...}      | eu-west-1
2   | workspace.put.kv.foo   | ws_1         | {"method":"PUT","path":...}       | eu-west-1
3   | workspace.delete       | ws_1         | {"method":"DELETE","path":...}    | eu-west-1
```

The classifier (`api._classify_mutation`) maps method+path to a `kind`
string so a downstream replicator can react differently per kind
(e.g. only replay KV writes; skip workflow state, which is regenerable).
The payload is intentionally lightweight — it captures method/path/status,
not the body, because writing every body to the log explodes storage.
For full-fidelity replay, replicate the underlying SQLite via
SQLite's `.dump` periodically and ship the diff.

Concretely: a "real" v1.0 production setup either:

- runs Postgres with streaming replication (recommended), or
- ships the entire SQLite file via `litestream` / `rsync --inplace` /
  S3 backup-and-restore, and uses `replication_log` only as an audit
  signal that captures *what writes happened* during the window.

The log's primary purpose in v1.0 is **introspection** — operators see
mutations during a window and can correlate them with their own
replication tooling. Full replay-from-log is left as an exercise; the
endpoint surface is sufficient for it.

### Identity — revocation polling (already in v0.6)

Identity's cross-region propagation lever is the existing
`GET /v1/revocations` poll loop (added in v0.6 as part of federated
revocation cache). Every service polls every region on a configurable
interval and merges the union of revoked JTIs. So a token revoked on
the EU primary lands on every replica within `revocation_poll_interval_seconds`
(default 60s).

This means Identity in v1.0 only needs the discovery surface
(`/v1/regions`) plus the replica-redirect middleware for `POST /v1/tokens`
and `POST /v1/tokens/revoke`. Verify (`POST /v1/tokens/verify`) is
idempotent and serves correctly from a replica — the verification path
is on the allowlist.

Tenant + key writes still go to the primary. JWKS endpoints
(`/v1/.well-known/jwks.json`) are read-only and serve from any replica;
the underlying key store is replicated via the same SQLite/Postgres
substrate workspace uses.

### Gateway — stateless region routing

Gateway has no replication primitive because it's stateless from a
durable-data perspective. The tool registry, audit log, and
OAuth-connection store *are* persistent, but they're operator-managed
configuration: changes are infrequent, replicated by config-management
tooling (k8s ConfigMaps, Helm, Terraform), not by row-level streaming.

What the gateway does in multi-region:

1. Exposes `/v1/regions` so the discovery surface is uniform.
2. Returns `421 REPLICA_READ_ONLY` (Misdirected Request) on mutating
   calls when in replica mode — the SDK retries against the primary.
3. Tags outgoing tool invocations with the region id (in audit and
   OTLP attributes) so operators can correlate cost / latency by region.

A replica gateway can still serve GET tool-list and dry-run calls.
Tool *invocation* (`POST /v1/invoke`) is a write (it logs an audit
event, possibly mutates external systems via the tool), so it 421s.

## Read-replica middleware

Each service has a small middleware that intercepts mutating requests
when `replication_mode=replica`:

- The methods POST / PUT / DELETE / PATCH return `421 REPLICA_READ_ONLY`.
  421 (Misdirected Request, RFC 7540 §9.1.2) is the right status: the
  request is syntactically fine but addressed to the wrong host.
- The response carries `X-Plinth-Primary-Region: <region_id>` and
  `X-Plinth-Primary-URL: <base_url>` so the SDK can retry transparently.
- A `Location` header points at the primary URL when configured (curl
  / browser-style follow-redirects).
- An allowlist exempts `/healthz`, `/v1/regions`, `/metrics`, and the
  replication-apply endpoint itself (replicas need to receive replays).

The allowlist is per-service:

| Service   | Replica-allowed mutating verbs                |
| --------- | --------------------------------------------- |
| Workspace | `POST /v1/admin/replication/apply`, `/healthz` |
| Identity  | `POST /v1/tokens/verify` (idempotent), `/healthz` |
| Gateway   | `/healthz`, `/v1/invoke/dry-run`              |

## SDK failover queue

The SDK `_http.py` (Python) and `http.ts` (TypeScript) maintain a
deterministic ordered list of `(region_id, base_url)` candidates. The
primary is always tried first. On any of:

- `httpx.ConnectError` / `httpx.ConnectTimeout` / fetch network error
- 5xx / 503 response
- 409 + `X-Plinth-Primary-Region` header pointing at a known fallback

…the next candidate runs. 4xx errors (other than the redirect 409) are
not retried — they surface unchanged to the caller. This matches how
operators want the failure semantics: "is the region up?" is a
network/server question, "is this request well-formed?" is not.

The failover loop dedupes by URL: if the redirect points at a region
we've already attempted, we surface the 409 instead of looping.

## Postgres path (out of scope for v1.0 code, in scope for ops doc)

The recommended Postgres production topology:

1. **Aurora Global** (or equivalent): a primary cluster in one region,
   read-replica clusters in others, with cross-region replication
   handled by the managed service. Lag typically <1s.
2. **Plinth wired to read-replica DB endpoints**: each replica region
   sets `PLINTH_DATABASE_URL` to its local read-replica's endpoint and
   `PLINTH_REPLICATION_MODE=replica`. The Plinth replica middleware
   handles the write redirect; the database layer handles the actual
   data replication.
3. **Failover** is a database-level operation. Promote a read-replica
   to primary, update DNS / config, flip the Plinth `replication_mode`
   to `primary` on the new region. The Plinth replication-log table
   is empty in this topology — Postgres replication did all the work.

For SQLite-based deployments, `litestream` is the obvious choice:
continuous WAL streaming to S3, with point-in-time-restore on the
replica side. Combined with the Plinth `replication_log` for write
introspection, this gives you a serviceable SQLite-multi-region setup
without heavyweight infrastructure.

## What's deliberately omitted

- **Multi-primary** — the replication log assumes a single writer.
  Two primaries would interleave `seq` numbers from two clocks; the
  apply endpoint would dedupe but not reorder. v1.0 is
  single-primary-eventual-replica only.
- **Conflict resolution** — there's no last-write-wins, vector-clock,
  or CRDT layer. If two replicas accept writes simultaneously (which
  shouldn't happen with the redirect middleware, but might during
  promotion races), the result is undefined.
- **Quorum reads** — replicas serve from local state. There's no
  "wait for primary to confirm" path; reads can be stale.
- **Anti-entropy** — the apply endpoint dedupes by `seq`, but there's
  no full-DB rsync or hash-tree comparison. If a replica falls
  arbitrarily behind, the operator restores from a primary backup.

## Backwards compatibility tally

| Knob                                | v0.6 default      | v1.0 default      | Effect on existing deploys |
| ----------------------------------- | ----------------- | ----------------- | -------------------------- |
| `PLINTH_REPLICATION_MODE`           | (didn't exist)    | `standalone`      | None — same behaviour      |
| `PLINTH_REGION_ID`                  | (didn't exist)    | `default`         | None — emitted in `/v1/regions` only |
| `PLINTH_REGION_PEERS`               | (didn't exist)    | `[]`              | None                       |
| `replication_log` table             | (didn't exist)    | created empty     | None — only written when `mode=primary` |
| `/v1/regions`                       | (didn't exist)    | new endpoint      | Additive                   |
| `/v1/admin/replication/*`           | (didn't exist)    | new endpoints     | Additive, admin-only       |
| `X-Plinth-Primary-Region` header    | (didn't exist)    | only on 409       | Additive                   |
| SDK `region` / `fallback_regions`   | (didn't exist)    | optional kwargs   | Additive — old code unchanged |

A v0.6 client talking to a v1.0 service sees the same wire protocol it
always saw, modulo the new endpoints. A v1.0 client talking to a v0.6
service has to set `replication_mode=standalone` (the default) and
gets identical behaviour.
