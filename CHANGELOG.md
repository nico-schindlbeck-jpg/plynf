# Changelog

All notable changes to Plinth are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is [SemVer](https://semver.org).

## [0.6.0] — 2026-05-07

Distribution & polish release. Federated revocation across multi-node Identity, Postgres advisory locks, migration rollback execution, generic resource locks, channel-schema migration helpers + DLQ batch ops, and visual workflow graph in the dashboard.

### Added
- **Federated revocation** (Identity → Workspace + Gateway): new `GET /v1/revocations` endpoint on Identity (cursor-based, paginated) plus per-service polling cache that refreshes every 60s. JWT verification now also rejects revoked JTIs locally without a network round-trip.
- **Postgres advisory locks** for the migration runner across all three services: `pg_advisory_lock(<service-hash>)` replaces fcntl flock when running on Postgres, allowing safe concurrent replicas.
- **Migration rollback execution**: new `<id>_rollback.sql` files; CLI `migrate --rollback-to <id>` and `--dry-run`; new `POST /v1/admin/migrations/rollback` endpoint. Atomic per-migration transactions with rollback-checksum verification.
- **Generic resource lock primitives** (Workspace): 5 new endpoints (`acquire`, `heartbeat`, `release`, `list`, `get`) on `/v1/workspaces/{ws}/locks/{name:path}`. Race-safe upsert. Lease reaper extended. SDK adds `ws.locks.held()` context manager (Python) and `ws.locks.withLock()` helper (TypeScript).
- **Channel schema migration helpers**: bulk `schema/check` (validate sample of messages against proposed schema), `replay-all` DLQ batch (with dry-run mode), `purge` DLQ by age. Workspace endpoints + Python/TS SDK methods + Dashboard "Replay all" / "Purge older than 24h" buttons.
- **Workflow visualization** in Dashboard: new `/workflows` and `/workflows/{wf_id}` SPA routes; horizontal SVG-based workflow graph with status-colored nodes (pending/running/completed/failed/cancelled), click-for-detail modal, auto-refresh with diff-based DOM updates, sortable+filterable list view, accessible (icon + text + color, keyboard-navigable).
- New `Lock` SDK type, `LockConflict` / `LockNotHeld` / `LockNotFound` typed exceptions.

### Changed
- Workspace: 465 tests (was 350): generic locks, schema-migration helpers, rollback, revocation cache
- Gateway: 396 tests (was 365): rollback, advisory-lock unit tests, revocation cache
- Identity: 166 tests (was 135): rollback, revocation endpoints, advisory-lock tests
- Python SDK: 267 tests (was 238): identity revocations, generic locks, schema-migration helpers
- TypeScript SDK: 118 tests (was 99): generic locks, schema-migration helpers
- Dashboard: 60 tests (was 36): workflow viz endpoints + tests, DLQ batch buttons
- Mock-MCP, MCP servers, worker, benchmarks: unchanged
- Total: **1503 Python + 118 TypeScript = 1621 tests passing** (was 1373 in v0.5)

### Configuration additions
- `PLINTH_REVOCATION_POLL_URL` (default ""), `PLINTH_REVOCATION_POLL_INTERVAL_SECONDS` (default 60), `PLINTH_REVOCATION_POLL_ENABLED` (default true) — workspace + gateway

### Backwards compatibility
- All v0.1–v0.5 demos still produce identical output (verified post-merge).
- `revocation_poll_url=""` default → no polling, no cache, services behave as v0.5.
- Postgres advisory locks no-op for SQLite path (default).
- Migration rollback never auto-runs; explicit CLI/endpoint only.
- Generic locks, schema-migration helpers, workflow viz: all additive. Existing surface unchanged.

### Known limitations (v0.6)
- TypeScript worker harness still pending (deferred to v0.7)
- Channel-schema-versioning UI in Dashboard is minimal (modal buttons only); richer schema-evolution wizard in v0.7

## [0.5.0] — 2026-05-07

Reliability & coordination-depth release. Real schema migrations, durable workflow execution with worker pools, Saga-style transactions, typed channels with DLQ, and stress benchmarks with load-shedding.

### Added
- **Migration framework** (workspace + gateway + identity) — versioned SQL files, CLI (`migrate`, `migrate --status`, `migrate --to <id>`, `migrate --create "msg"`), admin endpoints (`GET /v1/admin/migrations`, `POST /v1/admin/migrations/apply`), `schema_migrations` tracking table with sha256 checksums, file locking. 62 migration tests across 3 services.
- **Durable workflow executor** — worker pool with lease + heartbeat semantics. New endpoints: `lease`, `heartbeat`, `release` per step; `register`, `heartbeat`, `drain`, `list` for workers; `pending` and `expired` for visibility. Background lease reaper sweeps stale leases every 30s. New top-level package `worker/` with `plinth-workflow-worker` CLI. New SDK `@client.workflow_handler` decorator. 33 lease tests + 25 SDK tests + 13 worker tests.
- **Demo 05 — durable workflow** — agents register handlers via decorator; workers poll, lease, execute. Killing a worker mid-step → another worker picks up after the lease expires. Crash recovery verified end-to-end.
- **Workflow transactions with compensating actions** (Saga pattern) — group multiple tool calls; on failure, executed calls are rolled back via registered compensations in reverse order. Argument templates support `{result.field}` and `{seq.N.result.field}` placeholders. New endpoints: `POST /v1/transactions`, `/calls`, `/commit`, `/rollback`, `GET /v1/transactions/{id}`. 40 gateway tests + 27 SDK tests.
- **Typed channels** — optional JSON Schema per channel. Invalid messages route to a hidden `<channel>.deadletter` sub-channel. New endpoints: `POST /channels/{name}/schema`, `GET /deadletter`, `POST /deadletter/{id}/replay`, `DELETE /deadletter/{id}`. SDK `set_schema`, `deadletter`, `replay`. Dashboard DLQ panel + inspector modal. 28 workspace tests + 14 Python SDK + 11 TS SDK + 3 dashboard tests.
- **Stress benchmarks** — new top-level `benchmarks/` package with `plinth-bench` CLI (workspace_kv, workspace_files, workspace_snapshot, gateway_invoke_cached/cold, identity_token_issue). Open-loop ramp/hold/cooldown, p50/p95/p99 percentiles, JSON output, `compare.py` for markdown diffs. 11 tests.
- **Load-shedding middleware** (workspace + gateway) — bounded inflight + queue with 503 + Retry-After when overloaded. Health endpoints exempt. Stats endpoint. 21 tests.

### Changed
- Workspace: 350 tests (was 255 in v0.4): migrations, leases, channel schemas, GC, storage drivers all green
- Gateway: 365 tests (was 295): transactions, migrations, load-shed all green
- Identity: 135 tests (was 116): migrations green
- Python SDK: 238 tests (was 172): runtime + workers + transactions + channel schemas
- TypeScript SDK: 99 tests (was 88): channel schemas added
- Dashboard: 36 tests (was 33): DLQ panel
- Worker (NEW): 13 tests
- Benchmarks (NEW): 11 tests
- Total: **1274 Python + 99 TypeScript = 1373 tests passing** (was 1084 in v0.4)

### Stack additions
- `jsonschema>=4.20` (workspace, channel-schemas validation)
- `httpx[http2]>=0.27` (benchmarks)

### Configuration additions
- `PLINTH_AUTO_MIGRATE` (per service, default true) — auto-apply on startup
- `PLINTH_LEASE_REAPER_ENABLED`, `PLINTH_LEASE_REAPER_INTERVAL_SECONDS`, `PLINTH_WORKER_INACTIVE_TIMEOUT_SECONDS` (workspace)
- `PLINTH_LOAD_SHED_ENABLED`, `PLINTH_LOAD_SHED_MAX_INFLIGHT`, `PLINTH_LOAD_SHED_MAX_QUEUE`, `PLINTH_LOAD_SHED_RETRY_AFTER_SECONDS` (workspace + gateway)

### Backwards compatibility
All v0.1–v0.4 demos still produce identical numbers (verified post-merge). Migration runner detects existing databases and marks pre-v0.5 migrations as applied without re-running. Untyped channels behave exactly as before. Workflows without leases (the v0.2 in-process flow) continue to work; durable execution is opt-in by running workers. Load-shedding is opt-in (default disabled). Transactions are an entirely new endpoint family.

### Known limitations (v0.5)
- Federated revocation (multi-node Identity) deferred to v0.6
- Migration rollback (`apply_to <older>`) currently raises; rollback execution in v0.6
- Postgres advisory locks for migration runner not yet wired (SQLite uses fcntl flock)
- Worker handlers are Python-only; TypeScript-side worker harness in v0.6
- Two known-flaky JWT-tampering tests (random byte mutation that occasionally produces a still-valid signature) — non-deterministic, pre-existing

## [0.4.0] — 2026-05-07

Scale & operability release. Real Postgres backend, OpenTelemetry observability, two more OAuth providers, and production-grade RS256 with key rotation.

### Added
- **Postgres backend driver** (Workspace + Gateway + Identity) — runtime-selectable via `PLINTH_STORAGE_DRIVER=postgres` + `PLINTH_DATABASE_URL=postgres://...`. Per-service overrides (`PLINTH_WORKSPACE_DATABASE_URL`, etc.). Uses `asyncpg` directly. SQLite remains the default. 12 Postgres tests (skipped without `PLINTH_TEST_POSTGRES_URL`).
- **Workspace GC + Retention policies** — per-workspace retention rules (keep_versions, keep_days, keep_snapshots, delete_unreferenced_blobs). New endpoints: `POST /v1/workspaces/{id}/gc`, `GET/PUT /v1/workspaces/{id}/retention`, admin sweep `POST /v1/admin/gc`. Blob garbage collection (orphan detection) included. 33 GC tests.
- **OTLP event stream** (Gateway): emits every audit event as an OpenTelemetry Log to any OTLP/HTTP collector (Datadog, Tempo, Honeycomb, OTel Collector). Buffered, non-blocking, never crashes the gateway on emit failure. New endpoints: `GET /v1/observability/status`, `POST /v1/observability/flush`. 32 OTLP tests.
- **Slack OAuth provider + Slack MCP server** (port 7427) — 4 tools: `list_channels`, `post_message`, `list_messages`, `get_user`. Handles Slack's flat OAuth v2 response shape. 23 tests.
- **Linear OAuth provider + Linear MCP server** (port 7428) — 5 tools: `list_issues`, `get_issue`, `create_issue`, `update_issue`, `comment_on_issue`. GraphQL-backed. 27 tests.
- **RS256 + Key rotation** (Identity) — RSA-2048 keypair generation, AES-GCM at-rest encryption, automatic 30-day rotation, JWKS endpoint with last 3 non-expired keys. New endpoints: `GET /v1/keys`, `POST /v1/keys/rotate`, `DELETE /v1/keys/{kid}`. 45 keys/rotation tests.
- **JWKS verification** in Workspace + Gateway — fetch JWKS from Identity service on-demand, 5-minute cache, handles unknown-kid by re-fetching. Both verifiers accept HS256 + RS256 tokens simultaneously. 33 RS256 verifier tests.
- **Dashboard graph** — 60-minute time-series of tool calls per minute (vanilla Canvas, no chart lib) + OTLP status section.
- **SDK extensions** (Python + TypeScript): `IdentityClient.list_keys()`, `rotate_key()`, `expire_key()`, `get_key()`. SDK accepts both alg families transparently.

### Changed
- Workspace: 255 tests (+62), GC + retention policies + storage drivers
- Gateway: 295 tests (+78), OAuth providers + OTLP + RS256 verifier
- Identity: 116 tests (+53), RS256 keys + storage drivers
- Python SDK: 172 tests (+10)
- TypeScript SDK: 88 tests (+9)
- Dashboard: 33 tests (+8)
- Total: **996 Python + 88 TypeScript = 1084 tests passing** (was 814 in v0.3)

### Stack additions
- `asyncpg>=0.29` (Postgres driver, optional `[postgres]` extra in each service)
- `opentelemetry-api>=1.25`, `opentelemetry-sdk>=1.25`, `opentelemetry-exporter-otlp-proto-http>=1.25` (Gateway OTLP)

### Configuration additions
- `PLINTH_STORAGE_DRIVER`, `PLINTH_DATABASE_URL`, `PLINTH_*_DATABASE_URL`, `PLINTH_DB_POOL_*`
- `PLINTH_OTLP_ENABLED`, `PLINTH_OTLP_ENDPOINT`, `PLINTH_OTLP_*`
- `PLINTH_OAUTH_SLACK_*`, `PLINTH_OAUTH_LINEAR_*`
- `PLINTH_IDENTITY_JWT_ALG`, `PLINTH_IDENTITY_KEY_ROTATION_DAYS`, `PLINTH_IDENTITY_KEYS_DIR`, `PLINTH_IDENTITY_KEYS_ENCRYPTION_KEY`

### Backwards compatibility
All v0.1/v0.2/v0.3 tests pass. All 4 demos still produce identical numbers. Default `storage_driver=sqlite`, `otlp_enabled=false`, `jwt_alg=HS256` mean existing deployments behave unchanged.

### Known limitations (v0.4)
- No migration framework yet (schema applied idempotently); proper migrations in v0.5
- Federated revocation (multi-node Identity) still pending — single-node only
- Postgres tests run only when `PLINTH_TEST_POSTGRES_URL` env var is set
- TypeScript SDK doesn't emit OTLP events itself

## [0.3.0] — 2026-05-06

Production-credibility release. Adds real authentication, multi-tenancy, and the first real OAuth-backed tool integration.

### Added
- **Identity service** (new — port 7425): JWT capability tokens (HS256) with scope grammar, issuance/verification/revocation endpoints, JWKS endpoint, tenant management. 63 tests.
- **Multi-tenancy** across Workspace + Gateway: `tenant_id` columns added with default `"default"` for backwards compat. Tokens carry `tenant_id` claim; tenant-scoped queries enforced. Per-token rate-limit + cost-cap overrides via JWT claims.
- **JWT verification middleware** in Workspace + Gateway with three modes: `permissive` (back-compat default), `verify_local` (HS256 with shared secret), `verify_remote` (call identity service).
- **OAuth 2.0 Authorization Code Flow** (Gateway): PKCE-correct, GitHub-first. AES-256-GCM at-rest token encryption with auto-generated dev keys + clear production warnings. State store with TTL + replay protection. 47 new tests.
- **GitHub MCP server** (new — port 7426): real GitHub REST API integration. 7 tools: `list_issues`, `get_issue`, `create_issue`, `update_issue`, `comment_on_issue`, `get_repo`, `search_code`. Forwards `Authorization: Bearer …` from gateway. 42 tests.
- **GitHub issue-triage example** (`examples/04-github-issue-triage`): agent that classifies GitHub issues into bug/feature/question/spam buckets and writes a markdown triage report. Simulation mode bundles 10 issue fixtures; live mode uses real OAuth.
- **TypeScript SDK feature parity** (`@plinth/sdk` v0.3.0): full v0.2 channels + workflows surface, plus v0.3 identity client. Token counting via `gpt-tokenizer` (~150 KB BPE). 79 tests (was 28). README rewritten to mirror Python SDK.
- **Tenants endpoints** on Workspace + Gateway: `GET /v1/tenants`.
- **Identity SDK client** (Python): `client.identity.issue_token(...)`, `verify_token(...)`, `revoke_token(...)`, `list_tokens(...)`, `list_tenants()`, `create_tenant(...)`.

### Changed
- Workspace: 193 tests (+19), still 96 % coverage.
- Gateway: 217 tests (+61, +47 OAuth + +14 tenancy), 99 % coverage.
- Python SDK: 162 tests (+20), 94 % coverage.
- All demos still run; backwards compat verified (research-agent still measures 71 % token reduction).

### Stack additions
- `cryptography>=42.0` (Gateway, for OAuth at-rest encryption)
- `PyJWT[crypto]>=2.8` (Identity service)
- `gpt-tokenizer` (TypeScript SDK runtime dep)

### Configuration additions
- `PLINTH_IDENTITY_*` (port, jwt_secret, issuer, audience, default_ttl_seconds, max_ttl_seconds)
- `PLINTH_AUTH_REQUIRED`, `PLINTH_AUTH_MODE`, `PLINTH_IDENTITY_JWT_SECRET` (Workspace + Gateway)
- `PLINTH_OAUTH_GITHUB_*` (Gateway)
- `PLINTH_OAUTH_ENCRYPTION_KEY` (Gateway)

### Known limitations (v0.3)
- HS256 only (RS256 + key rotation in v0.4)
- Single-node revocation list (federated revocation in v0.4)
- Only GitHub provider implemented (Slack, Linear in v0.4)
- TypeScript SDK doesn't yet emit OTLP events
- Postgres backend still pending (v0.4)

## [0.2.0] — 2026-05-06

MVP release. Adds the differentiating coordination primitives, production-safety controls, and the demoable observability surface.

### Added
- **Channels primitive** (Workspace service) — durable message queues for multi-agent handoffs. Per-workspace, monotonically sequenced, with optional consumer cursors and `peek` semantics. 6 new endpoints.
- **Workflows primitive** (Workspace service) — checkpointed step sequences with full lifecycle (pending → running → completed | failed | cancelled), automatic resume-from-snapshot. 7 new endpoints.
- **Rate limiting + cost caps** (Gateway) — per-`agent_id` token-bucket rate limiter (configurable RPM + burst) and rolling-window cost ceilings (1h + 24h). Returns 429 with `Retry-After` header. Override per agent via new `/v1/limits/{agent_id}` endpoints. Disable globally via `PLINTH_RATE_LIMITS_ENABLED=false`.
- **Dashboard service** (new, port 7424) — single-page HTML/JS app + read-only proxy. Shows workspaces, audit log, cache stats, cost rollups, and per-workspace KV/snapshots/channels/workflows. Polls every 5 s.
- **Multi-agent-handoff demo** (`examples/02-multi-agent-handoff`) — Researcher → Writer → Reviewer pipeline communicating via channels. Demonstrates structured handoffs with bounded prompt sizes (~8.7k tokens total across 3 agents).
- **Resumable-workflow demo** (`examples/03-resumable-workflow`) — 6-step deep-research pipeline that crashes mid-flight, then resumes from snapshot. Demonstrates ~32 % token saving versus restart-from-scratch.
- **Python SDK extensions** — `ws.channels` (send/receive/wait/ack/list/delete) and `ws.workflows` (create/get_or_create/list, plus `WorkflowHandle` with `start_step`, `complete_step`, `fail_step`, `cancel`, `resume_info`, `refresh`).
- **Top-level integration** — `make serve` now also starts the dashboard; `make demo-handoff` and `make demo-resume` for the new examples; `docker-compose.yml` extended with the dashboard service; `.claude/launch.json` for `preview_start`.

### Changed
- `services/workspace`: 174 tests (+63), still 96 % coverage.
- `services/gateway`: 156 tests (+86), 99 % coverage; `/v1/invoke` now enforces limits when `agent_id` is provided.
- `sdk/python`: 142 tests (+50), 94 % coverage; new `InvalidWorkflowStep` and `*NotFound` exceptions for the new endpoints; `InvalidStepName` kept as backwards-compat alias.

### Fixed
- Workspace `kv/{key}` route now uses `:path` modifier so hierarchical keys (`"sources/index"`) work.
- SDK URL-encodes KV keys and file paths.
- `HTTPToolBackend` in the demo unwraps mock-mcp's outer `result` envelope.
- `make serve` uses `scripts/_spawn.py` (Python `start_new_session`) so services survive on macOS without `setsid`.

### Stack additions
- Dashboard: vanilla HTML/JS (no framework), FastAPI proxy to upstream services.

## [0.1.0] — 2026-05-05

Initial proof-of-concept release. Working end-to-end slice of the agent-native substrate.

### Added
- **Workspace service** — versioned KV + file storage with snapshots and branches over SQLite + content-addressed blobs. 27 files, 111 tests, 96% coverage.
- **Tool Gateway service** — MCP-compatible HTTP proxy with caching, audit log, mock OAuth pass-through, and dry-run mode. 30 files, 70 tests, 99% coverage.
- **Python SDK (`plinth`)** — full client surface for workspaces, KV, files, snapshots, branches, tool invocation, token counting (tiktoken cl100k_base), and an `@agent` decorator. 23 files, 92 tests, 99% coverage.
- **TypeScript SDK (`@plinth/sdk`)** — skeleton at parity for basic operations. 15 files, 28 tests, strict-mode TypeScript.
- **Mock MCP server** — 6 demo tools (`web.fetch`, `web.search`, `fs.read`, `fs.write`, `notes.add`, `notes.list`) with bundled fixtures for 3 topics, all offline-capable. 13 files, 33 tests, 96% coverage.
- **Headline demo** (`examples/01-research-agent`) — baseline vs Plinth-enabled research agent with token-exact comparison via tiktoken. **Measured 71%+ token reduction** across all 3 bundled topics.
- **Specifications** — OpenAPI 3.1 specs for both services (validated with `openapi-spec-validator`), JSON Schemas for the observability event format and capability tokens, gRPC proto sketches for v1.0.
- **Documentation** — 6 architecture sub-documents (1645–2108 words each), 6 ADRs (1064–1535 words each), 5 Mermaid diagrams.
- **Integration** — `Makefile` (install/test/serve/demo/stop), `docker-compose.yml`, healthcheck script, GitHub Actions CI workflow.
- **Repo hygiene** — README, CONTRACTS, CONVENTIONS, ARCHITECTURE, ROADMAP, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, Apache 2.0 LICENSE.

### Known limitations
- Single-node only (no clustering, no multi-region)
- Bearer-token auth is a placeholder (any non-empty string accepted)
- Real OAuth flows are mocked
- Coordination primitives, observability event stream, and capability-token issuance are designed (`docs/architecture/`) but not yet implemented
- Mock MCP fixtures are 600–800 words per source; spec target was 1200–1800 (deferred to v0.2)

### Stack
Python 3.11+ for services + Python SDK; TypeScript 5.4+ for the TS SDK; FastAPI + uvicorn + aiosqlite + pydantic v2 + tiktoken; vitest for TS tests.

[0.6.0]: https://github.com/your-org/plinth/releases/tag/v0.6.0
[0.5.0]: https://github.com/your-org/plinth/releases/tag/v0.5.0
[0.4.0]: https://github.com/your-org/plinth/releases/tag/v0.4.0
[0.3.0]: https://github.com/your-org/plinth/releases/tag/v0.3.0
[0.2.0]: https://github.com/your-org/plinth/releases/tag/v0.2.0
[0.1.0]: https://github.com/your-org/plinth/releases/tag/v0.1.0
