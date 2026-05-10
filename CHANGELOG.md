# Changelog

All notable changes to Plinth are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is [SemVer](https://semver.org).

## [1.2.1] — 2026-05-10

Patch release: TypeScript SDK reaches full LLM-layer parity with the Python SDK + CI hotfix.

### Added
- **`@plinth/sdk` LLM namespace** (TypeScript) — `client.llm.complete()` / `stream()` with provider abstraction. Mirrors the Python surface shipped in v1.2.
  - `AnthropicProvider` / `OpenAIProvider` / `MockProvider` with the same pricing tables.
  - Vendor SDKs as optional peer-dependencies (`@anthropic-ai/sdk`, `openai`) — base package unaffected.
  - Auto-detect from `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` env.
  - Retry+backoff on 429 + 5xx with `Retry-After` honoring.
  - Cost tracking via the gateway's `POST /v1/audit/record-llm` endpoint.
- TS SDK: 194 tests (was 144), +50 LLM tests.

### Fixed
- **CI**: Postgres-driver tests were failing with `ModuleNotFoundError: asyncpg` because CI installed only `[dev]` extra. Now installs `[dev,postgres]` for workspace + gateway + identity so live-Postgres tests against the service container actually run.
- **CI contract test**: `test_workspace_documented_status_codes_exist_in_app` flagged 401/500 as missing because they're emitted by middleware/exception-handlers, not router decorators. Now treats those two codes as middleware-emitted and skips them.

### Changed
- TypeScript SDK: 194 tests (was 144)
- Total: **2406 Python + 194 TS-SDK + 29 TS-Worker = 2629 tests passing** (was 2579)

### Backwards compatibility
- All v1.2 endpoints unchanged
- TS `client.llm` is a new namespace
- LLM peer-deps are opt-in (`npm install @plinth/sdk @anthropic-ai/sdk`)
- API v1 contract preserved

## [1.2.0] — 2026-05-10

LLM Layer in the Python SDK — closes the #1 functional gap. Today every example used a mock LLM. v1.2 adds `client.llm` with provider abstraction (Anthropic, OpenAI, Mock), streaming, retry+backoff, cost tracking integrated into the existing audit pipeline.

### Added
- **`plinth.llm` namespace** — `LLMClient` facade wrapping a provider, retry loop, cost tracking, audit recording. `client.llm.complete(...)` / `stream(...)` / `acomplete(...)` / `astream(...)`.
- **`MockProvider`** — pure-Python, deterministic, used by all SDK tests + bundled demo. No real API calls in tests.
- **`AnthropicProvider`** — wraps `anthropic` package conditionally (opt-in via `pip install plinth[anthropic]`). Hardcoded pricing for `claude-sonnet-4-5`, `claude-opus-4-5`, `claude-haiku-4-5`. Streaming via `messages.stream()`.
- **`OpenAIProvider`** — wraps `openai` package conditionally (`pip install plinth[openai]`). Pricing for gpt-5, gpt-5-mini, gpt-5-nano. Streaming via `chat.completions.create(stream=True)`.
- **Auto-detection** — if `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set and no provider is explicitly configured, first call lazy-builds the matching provider.
- **Retry logic** — exponential backoff on 429 (rate-limited) + 5xx; honors `Retry-After` header. Never retries 4xx-other (auth, invalid request).
- **Cost tracking** — every successful LLM call records to `POST /v1/audit/record-llm` (NEW gateway endpoint). Audit row has `tool_id="llm.<provider>"`, `cost_estimate_usd` populated → existing Prometheus metrics + dashboard cost rollups pick up direct-LLM spend automatically.
- **Demo 06 — `llm-research-agent`** — version of demo 01 using `client.llm` (MockProvider default; `--mode=live` switches to AnthropicProvider with `ANTHROPIC_API_KEY`).
- **`pip install plinth[anthropic]` / `[openai]` / `[all]` extras** — opt-in vendor SDKs (no base-dep bloat).
- **`POST /v1/audit/record-llm` endpoint** on Gateway — synthesises audit row for direct-LLM calls (no tool proxy involved).

### Changed
- python SDK: 368 tests (was 301) — +67 LLM tests
- gateway: 520 tests (was 517) — +3 LLM-audit endpoint tests
- Total: **2406 Python + 144 TS-SDK + 29 TS-Worker = 2579 tests passing** (was 2312 in v1.1)

### Backwards compatibility
- All v1.1 endpoints unchanged
- `client.llm` is a new namespace — code that doesn't use it is unaffected
- LLM extras are opt-in (`plinth[anthropic]` / `[openai]`)
- Audit endpoint addition is purely additive
- API v1 contract preserved

All v0.1–v1.1 demos produce unchanged output.

## [1.1.0] — 2026-05-10

Engineering-debt sweep + strategic adds. v1.1 is purely additive on v1.0 GA — the API v1 contract is fully preserved.

### Added

- **Pluggable `CoordinationBackend`** (memory + Redis) used by Identity revocation cache, Gateway rate-limits + tenant cost-caps. Single env var (`PLINTH_COORDINATION_BACKEND=redis`) flips multiple replicas to cluster-shared state. `MemoryBackend` is the default → v1.0 behavior unchanged. 88 new coordination tests across workspace + gateway + identity.
- **OTel public-logs migration** — try-import wrapper prefers `opentelemetry.sdk.logs`, falls back to `opentelemetry.sdk._logs` (still canonical through 1.41). `<1.30` pin lifted across all 3 OTel deps in gateway. 43/43 OTel tests green post-migration.
- **Workflow retries with exponential backoff + DLQ** — `WorkflowStep` gains `max_attempts`, `retry_policy`, `retry_initial_delay_seconds`, `retry_max_delay_seconds`, `retry_jitter`, `next_retry_at`. New `workflow_dlq` table + 3 DLQ endpoints (list, replay, delete). Failed steps that exhaust attempts route to per-workflow DLQ.
- **Migration rollback files for every existing migration** across workspace + gateway + identity. 8 new `<id>_rollback.sql` files. Rollback CLI + endpoint can now actually execute reversal.
- **Lease-reaper jitter** — ±25% uniform random jitter on the periodic lease-reaper loop prevents thundering-herd when multiple workspace replicas wake at the same wall-clock second.
- **Notion MCP server** (port 7429) with 7 tools: `notion.search`, `get_page`, `create_page`, `update_page`, `append_block`, `list_databases`, `query_database`. 46 tests.
- **Google Workspace MCP server** (port 7430) with 8 tools: `google.drive_search`, `drive_read`, `docs_create`, `docs_append`, `sheets_read`, `sheets_append_row`, `calendar_list_events`, `gmail_list_messages`. PKCE-correct OAuth flow. 53 tests.
- **Gateway OAuth providers extended** — Notion (no PKCE) + Google Workspace (PKCE + refresh tokens). Existing GitHub/Slack/Linear flows unchanged. 22 new gateway OAuth tests.
- **CI hardening**:
  - `python` matrix expanded from 4 → 12 suites × 2 Python versions (workspace, gateway, identity, sdk-python, mock-mcp, dashboard, github-mcp, slack-mcp, linear-mcp, worker, benchmarks, cli)
  - Postgres service container in CI with `PLINTH_TEST_POSTGRES_URL` set — Postgres tests now execute (no longer skip)
  - New `typescript-worker` job for `worker-ts/`
  - New CodeQL workflow (Python + JS/TS, security-extended queries, weekly schedule)
  - New Dependabot config (26 update entries: pip + npm + docker + github-actions)
  - Issue templates (bug, feature, question), PR template, CODEOWNERS
- **Real benchmark numbers** — `benchmarks/results/baseline-v1.1.json` populated against running stack on Apple M4. README "Performance" table replaced with measured p50/p95/p99 across 6 workloads. Per-second buckets persisted to `benchmarks/results/raw-v1.1/`.

### Changed
- workspace: 576 tests (was 532 in v1.0): +retry +DLQ +coordination
- gateway: 517 tests (was 452): +Notion/Google OAuth +coordination +OTel
- identity: 239 tests (was 218): +coordination
- python SDK: 301 tests (was 292): +retry config + DLQ access
- typescript SDK: 144 tests (was 136): +retry config + DLQ access
- mcp-servers/notion: NEW, 46 tests
- mcp-servers/google-workspace: NEW, 53 tests
- Total: **2139 Python + 144 TS-SDK + 29 TS-Worker = 2312 tests passing** (was 2066 in v1.0)

### Stack additions
- `redis>=5.0` — coordination backend (pure-Python, base dep across services)
- `fakeredis>=2.20` — dev dep for cluster-shared coordination tests
- `opentelemetry-*>=1.25` — pin lifted (`<1.30` removed)

### Backwards compatibility
- All v1.0 endpoints unchanged
- `coordination_backend=memory` default → v1.0 deployments behave identically
- Workflow retries opt-in (`max_attempts=1` is the default and identical to v1.0)
- DLQ endpoints are additive
- Notion + Google MCP servers are new ports — existing infra unchanged
- API v1 contract preserved; deprecation policy unchanged

All v0.1–v1.0 demos produce unchanged output (verified — research-agent: 70.4% reduction, multi-agent: 8737 tokens, resume: 5841 saved, triage: 10 issues classified).

## [1.0.0] — 2026-05-08

**General Availability release.** Compresses the v0.7–v0.9 trajectory into one coherent ship: stable API guarantees, multi-region scaffolding, per-tenant resource quotas, GDPR compliance scaffolding, tamper-evident audit chain, unified operator CLI, comprehensive Prometheus + OTLP observability, production deployment artifacts (k8s + Helm + Terraform), threat model.

### Added

- **Per-tenant resource quotas** (Identity-driven, enforced in Workspace + Gateway): max workspaces, max storage GB, max channels per workspace, max workflows per workspace, max active tokens, max OAuth connections, max cost USD/day + USD/month, max invocations/minute. Returns 429 + `QUOTA_EXCEEDED` envelope. Opt-in via `PLINTH_QUOTAS_ENABLED=true` (default false to preserve v0.6 demos).
- **Tenant Admin UI** in Dashboard — `/tenants` route with create/edit/delete + quota editor + per-tenant detail page (members, OAuth connections, audit, cost).
- **Channel Schema Evolution Wizard** — Dashboard modal for `set_schema`, `schema/check`, `replay-all` DLQ, `purge`. JSON validation in-browser.
- **Multi-region scaffolding** — `region_id` + `region_replication_mode` settings, peer probe, `GET /v1/regions` per service, replica-mode middleware that returns 421 (Misdirected Request) with `X-Plinth-Primary-Region` + `X-Plinth-Primary-URL` for mutating calls. SDKs (Python + TypeScript) auto-retry once against primary on 421; fall back across `fallback_regions` on connection errors.
- **Unified `plinth` CLI** (new top-level `cli/` package, `pip install plinth-cli`): commands `services`, `migrate`, `workflow`, `audit`, `tenant`, `bench`, `health`, `completion`. Click + Rich output. `~/.plinth/config.toml` profiles + env-var override. 101 tests.
- **GDPR data export** — async export job pipeline: Identity coordinates, Workspace + Gateway each expose `/v1/admin/tenants/{id}/data_dump` (admin-scoped). Output: ZIP with workspaces, KV/files JSONL, audit, oauth (token-redacted), tenants, quotas. 7-day expiry.
- **GDPR data deletion** — two-phase confirm (request → confirm token → cascade delete across services). Block if pending exports.
- **Tamper-evident audit chain** in Gateway — every audit event now carries `prev_hash` + `event_hash` (SHA-256 over canonical-JSON). New endpoint `GET /v1/audit/verify` walks the chain and reports first hash mismatch.
- **Threat model** — `docs/threat-model.md`, 2,623 words, STRIDE-based, 45 specifically numbered threats, 8 attacker classes.
- **Compliance operator guide** — `docs/compliance.md` mapping SOC2 controls to Plinth features, GDPR walkthrough, audit-chain how-to, key-rotation cookbook.
- **Production deployment artifacts**:
  - `deploy/k8s/` — namespace + Deployments + Services + ConfigMaps + Secrets + Ingress + kustomization for all 8 services + Postgres (optional StatefulSet)
  - `deploy/helm/plinth/` — Helm chart 1.0.0 with values, values-prod.yaml, ingress, HPA, NetworkPolicy, ServiceAccount, helm-test pod
  - `deploy/terraform/aws-example/` — EKS + RDS + S3 + IAM module
  - `.github/workflows/release.yml` — GHCR multi-arch build + push for all 8 services on tag
- **API v1 stability promise** — `docs/API_STABILITY.md` codifies additive-only guarantee, 12-month deprecation policy via `Deprecation:` + `Sunset:` headers, scope of stability.
- **Contract tests** — `tests/contract/` runs against running services to verify response shapes match OpenAPI specs. Includes `scripts/openapi_diff.py` for breaking-change detection in CI.
- **Comprehensive Prometheus exporter** — `/metrics` endpoint on every service: `plinth_http_requests_total`, `plinth_http_request_duration_seconds`, `plinth_tool_invocations_total`, `plinth_tool_invocation_cost_usd_total`, `plinth_workflow_steps_total`, `plinth_lease_acquired_total`, `plinth_workers_active`, `plinth_load_shed_total`, `plinth_oauth_connections`, `plinth_rate_limit_rejections_total`, `plinth_tokens_issued_total`, `plinth_active_tokens`, `plinth_mcp_invocations_total`. Path normalization for high-cardinality routes.
- **Dashboard time-series graphs** — 24h + 7d trends for cost, latency p99, error rate, cache hit rate. Pure SVG, no external libs.
- **SLO definitions** — `docs/slos.md`, 26 specific SLOs across workspace/gateway/identity/cross-service with measurement methodology + burn-rate alerts + page-vs-ticket policy.
- **Observability operator guide** — `docs/observability.md`, 2,177 words, 10 PromQL alert recipes, Grafana dashboard layout, OTLP receivers (Datadog/Honeycomb/Tempo).

### Changed

- README, OVERVIEW, EXECUTIVE_SUMMARY, PLAYBOOK, TECHNICAL_REFERENCE all updated for v1.0 surface.
- Test totals: **2066 tests passing** (1901 Python + 136 TS-SDK + 29 TS-Worker). 15 Postgres tests skipped.
- Repo: ~9 services, 8 deployable units, 5 demos, 13 OpenAPI specs validated, ~120k LOC.

### Stack additions
- `prometheus-client` (zero-dep custom registry actually — see Notes)
- Click + Rich (CLI)
- No new heavy runtime deps

### Backwards compatibility
- Every v0.x demo produces unchanged output
- All new endpoints additive
- Quotas opt-in; multi-region opt-in (standalone is default); audit chain backward-compatible (NULL hashes for legacy rows)
- Existing 1650 v0.6.1 tests still pass

### Notes
- Prometheus exporter uses an in-tree zero-dep `MetricsRegistry` rather than the `prometheus-client` package — output is canonical Prometheus exposition format, verified parseable by `prometheus_client.parser`. Decision keeps the runtime deps minimal.
- OpenTelemetry SDK pinned `<1.30` (1.30 reorganized internal `_logs` API and silently breaks our `LoggerProvider` wiring; will migrate to public API in v1.1).

## [0.6.1] — 2026-05-08

Polish patch: ships the deferred TypeScript worker harness so JS-shop developers reach feature-parity with the Python `plinth-workflow-worker`.

### Added
- **TypeScript worker harness** (new top-level package `worker-ts/`, `@plinth/workflow-worker`): `WorkflowRuntime` (handler registry), `Worker` class with race-safe lease + heartbeat + graceful shutdown, `plinth-workflow-worker` CLI binary mirroring the Python flag set.
- **TS SDK additions** (additive only): new `WorkersClient` (register/heartbeat/drain/list); workflow methods `pendingSteps`, `expiredLeases`, `leaseStep`, `heartbeatStep`, `releaseStep`; new types `Lease`, `WorkerRecord`; new typed errors `LeaseConflictError`, `LeaseNotHeldError`, `WorkerNotFoundError`, `NoHandlerError`.
- **Example 05 TypeScript variant**: `handlers.ts` + `start-workflow.ts` + per-example `tsconfig.json`; README updated with TS-side run instructions. The Python files remain unchanged and fully functional.
- **GitHub Release tag** for v0.6.0 with complete release notes.

### Changed
- TypeScript SDK: 118 tests (unchanged — additions only added new code paths covered by new worker tests)
- New `worker-ts` package: 29 tests (12 runtime + 17 worker incl. CLI)
- Total: 1503 Python + 118 TS-SDK + 29 TS-Worker = **1650 tests passing**

### Backwards compatibility
- All Python tests unchanged
- TS SDK changes are purely additive (no signature changes)
- Example 05 Python path unchanged
- New `worker-ts` package is opt-in (separate `npm install`)

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

[1.2.1]: https://github.com/nico-schindlbeck-jpg/plinth/releases/tag/v1.2.1
[1.2.0]: https://github.com/nico-schindlbeck-jpg/plinth/releases/tag/v1.2.0
[1.1.0]: https://github.com/nico-schindlbeck-jpg/plinth/releases/tag/v1.1.0
[1.0.0]: https://github.com/nico-schindlbeck-jpg/plinth/releases/tag/v1.0.0
[0.6.1]: https://github.com/nico-schindlbeck-jpg/plinth/releases/tag/v0.6.1
[0.6.0]: https://github.com/nico-schindlbeck-jpg/plinth/releases/tag/v0.6.0
[0.5.0]: https://github.com/your-org/plinth/releases/tag/v0.5.0
[0.4.0]: https://github.com/your-org/plinth/releases/tag/v0.4.0
[0.3.0]: https://github.com/your-org/plinth/releases/tag/v0.3.0
[0.2.0]: https://github.com/your-org/plinth/releases/tag/v0.2.0
[0.1.0]: https://github.com/your-org/plinth/releases/tag/v0.1.0
