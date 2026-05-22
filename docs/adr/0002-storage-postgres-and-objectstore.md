# ADR 0002: Storage — SQLite for v0.1, Postgres + Object Store for v1.0

- **Status**: Accepted
- **Date**: 2026-05-05
- **Deciders**: The Plynf Authors

## Context

Plynf has two qualitatively different kinds of state:

1. **Metadata** — workspace records, KV entries with versions, file entries with content references, snapshots, branches, tools, audit events, cache lookups. Small rows, transactional, queried by indexed columns. Plynf's contract correctness depends on these being ACID.
2. **Blobs** — file content bytes referenced from `FileEntry` rows. Potentially large (KBs to MBs to GBs), append-only, content-addressed by SHA256, never updated in place.

These have completely different access and scaling profiles. Putting them in the same store would either underutilize blob storage (Postgres bytea is fine but expensive at scale) or under-protect metadata (object stores have no transactions). The decision is two-part: pick a metadata store and pick a blob store, picking each twice — once for v0.1 (zero-ops PoC), once for v1.0 (production scale).

Constraints:
- v0.1 must run with `make install && make serve` on a developer's laptop. No external services.
- v0.1 is single-node; v1.0 is multi-replica per service.
- Python-native libraries available for whatever we pick (ADR 0001).
- We expect customer environments to range from "self-host on a k8s cluster" to "managed cloud". Metadata store needs to be widely available across both; same for blob store.
- Tests must be fast and not require external services.

## Decision

### v0.1 — SQLite + local filesystem

- **Metadata**: SQLite (one file per service: `workspace.db`, `gateway.db`).
- **Blobs**: local filesystem at `$PLINTH_DATA_DIR/blobs/<sha256>`, content-addressed.
- WAL mode enabled (concurrent reads + single writer).
- `aiosqlite` for async access; `asyncio.to_thread` wrappers as fallback.
- One DB per service; the workspace and gateway services do not share a DB even in v0.1.

### v1.0 — Postgres + S3-compatible object store

- **Metadata**: Postgres 15+. Schema-compatible with the SQLite v0.1 schema (modulo type widenings).
- **Blobs**: S3-compatible object store. Cloudflare R2, Tigris, AWS S3, MinIO — anything that speaks S3. We target the smallest common-denominator API (PutObject, GetObject, HeadObject, DeleteObject, multipart for large blobs).
- `asyncpg` for Postgres; `aioboto3` or `boto3` (in a thread pool) for S3.
- Cache and audit stay in Postgres for v1.0; only file-blob bytes leave for object storage.

The path between them is **incremental** — see Migration below.

## Consequences

### Positive

- **Zero-ops dev loop.** SQLite plus a directory means `make install` works, tests are fast, single-binary distribution is plausible. Newcomers get a working Plynf in minutes without standing up Postgres.
- **Real prod scaling.** Postgres handles the metadata workload comfortably to multi-million workspaces and well past it. S3 handles blob volumes that would crush any single-DB store, and is independently scalable from the metadata tier.
- **Storage is the right cost shape.** Blobs in S3-compatible storage are roughly $0.015/GB/month on R2; Postgres rows are sized for transactional metadata. A workspace with 100GB of files but 100K rows of KV/metadata is not weirdly priced.
- **Independence of failure domains.** Postgres outage doesn't lose blobs; S3 hiccup doesn't corrupt metadata. A subtle thing — content-addressed blobs mean we can re-reference an existing blob from new metadata without re-uploading.
- **Familiarity.** Both Postgres and S3 are understood by every operations team we'd want to sell to. SOC2 audit conversations are easy.
- **The split anticipates future work.** Audit and cache could move to specialised stores at v2.0+ (a time-series DB for audit, Redis for cache). Because they live in the metadata tier and are accessed through narrow service-internal interfaces, that's an internal refactor, not an architectural change.

### Negative / Trade-offs

- **SQLite has limits we will hit.** Single-writer constraint, weak networked-access story, no replication. The PoC accepts this; the migration to Postgres is the entire point of having a v1.0.
- **SQLite vs Postgres SQL dialect.** Not perfectly compatible — JSONB indexes, generated columns, server-side aggregations differ. We mitigate by writing migrations in a compatible subset and by routing all SQL through service-internal repositories that target a small dialect surface. A test matrix runs both backends in CI from v0.2 onwards.
- **Object store coordination.** A blob is "live" the moment the metadata row is committed. If we crash *between* PutObject and metadata-INSERT, we leak a blob (cheap, garbage-collectable). If we crash *between* metadata-INSERT and PutObject, we have a dangling reference (bad). We address this by always uploading the blob first, then committing the metadata row only after PutObject succeeds — and by treating any blob that no metadata row points to as garbage.
- **Multipart uploads complicate the picture.** Large blobs can't be uploaded atomically. We use S3 multipart and complete the upload before INSERT, accepting that abort-cleanup of failed multiparts is a background sweep.
- **Cost at small scale.** A managed Postgres + S3 stack starts around $30–60/month for the smallest sane deployment. v0.1's free local-FS posture is materially cheaper. We accept this for production deployments and will offer a "frugal" deployment recipe (Postgres on the same host as the app, MinIO, k8s) in the docs.

## Alternatives Considered

### Skip SQLite, ship Postgres-only from v0.1

Tempting (one stack, no migration). Rejected because:
- It contradicts the "five-minute quickstart" pitch. A `docker compose up` with Postgres + MinIO + workspace + gateway works but adds friction we don't need at PoC.
- Tests would need a Postgres harness (`testcontainers` or `pytest-postgresql`); the per-test-startup cost matters when we run hundreds of unit tests.
- The schema we ultimately want for Postgres is meaningfully different from the v0.1 schema (we'll add column types, indices, and partitions when we know the workload). Building it now is premature.

### DuckDB instead of SQLite

DuckDB is excellent for analytics and would be lovely for audit-event queries. But DuckDB's transactional story (single-writer, file-locked) is no better than SQLite, and its primary value is OLAP. Our hot path is OLTP. Future option for the analytics side of audit (arch doc 05 mentions ClickHouse / Timescale for that role at scale).

### Postgres-only including blobs (bytea / large objects)

Considered. Rejected: bytea storage at multi-GB scales is materially more expensive than S3 in any cloud, and Postgres replication-bandwidth scales with row size — a 1GB blob update is a 1GB write to the replica. S3 handles the blob workload much better. The only argument for unified Postgres is "one less thing to operate"; we judge the operational cost of S3-compatible storage to be very low (R2/Tigris are well-managed, MinIO is well-understood for self-host).

### LibSQL / Turso (replicated SQLite)

Genuinely interesting. LibSQL gives you SQLite semantics with an embedded edge-replication story. We took a serious look. Rejected for v1.0 because:
- Postgres is more familiar to our buyer audience.
- Postgres has stronger column-type enforcement and richer indexes (partial, GIN/GIST, expression indexes).
- LibSQL's multi-writer story is still maturing as of 2026.

We'll keep an eye on it. If it converges, swapping the metadata tier in a future version is plausible.

### MongoDB / DynamoDB / generic document store

Rejected. Our data model is naturally relational (workspace ↔ kv_entries ↔ snapshots ↔ branches with foreign-key invariants). Document stores buy nothing and lose join semantics we use in audit and history queries. The "schemaless" benefit is illusory at our level of structure — we have a strict schema (CONTRACTS.md), and we want the database to enforce it.

### Filesystem-only (no metadata store), Git-style

Looked into. Storing JSON files for KV entries and using directory structure for keys is appealingly simple but makes operations like "latest version of every key in workspace W" an O(N) directory walk. At any non-trivial scale, untenable. Also, we want SQL for audit queries.

## Migration Path (v0.1 → v1.0)

The path is designed to be uneventful:

1. **Schema-compatible writes from v0.2.** When we add Postgres support, the SQLite schema is ported as-is. Any v0.2-introduced new columns are added in both backends.
2. **Bulk export tool.** `plinth export --to=postgres://...` reads SQLite and writes Postgres + uploads blobs to S3. Idempotent (ON CONFLICT DO NOTHING + content-addressed blobs).
3. **Incremental sync option.** For zero-downtime migrations: dual-write SQLite + Postgres in a transition window, then cut over reads.
4. **Verify tool.** `plinth verify --src=sqlite --dst=postgres` walks both, hashes the rows, and checks parity. SHA-of-rows is fast for the metadata; SHA of file content is content-addressed by definition.
5. **Documented runbook** in [`docs/architecture/01-system-overview.md`](../architecture/01-system-overview.md) §2.2 plus a dedicated `docs/operations/migration-v0.1-to-v1.0.md` (post-v0.1 deliverable).

## Notes / Links

- Workspace storage details: [`docs/architecture/02-workspace-design.md`](../architecture/02-workspace-design.md)
- Gateway storage uses the same metadata tier: [`docs/architecture/03-tool-gateway-design.md`](../architecture/03-tool-gateway-design.md) §4 (audit) and §3 (cache)
- Multi-tenancy implications for storage: [ADR 0006](./0006-multitenancy-model.md)
- Production deployment shape: [`docs/architecture/01-system-overview.md`](../architecture/01-system-overview.md) §2.2
