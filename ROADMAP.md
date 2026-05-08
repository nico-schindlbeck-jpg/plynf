# Plinth Roadmap

A living document. Reorder by community pull and demo data.

## v0.1 — PoC ✅ (released 2026-05-05)

**Goal**: prove the primitives. Show measurable agent token reduction.

- [x] Workspace service: KV + files + snapshots + branches, all versioned
- [x] Tool gateway: MCP proxy, caching, audit, mock auth
- [x] Python SDK
- [x] TypeScript SDK skeleton
- [x] Mock MCP server (6 tools, offline-capable)
- [x] Research-agent demo (baseline vs Plinth, 71 % token reduction measured)
- [x] Docker Compose
- [x] OpenAPI specs, ADRs, architecture docs

## v0.2 — MVP ✅ (released 2026-05-06)

**Goal**: differentiation + production safety + observability surface.

- [x] **Channels primitive** — durable per-workspace message queues with consumer cursors
- [x] **Workflows primitive** — checkpointed step sequences with resume-from-snapshot
- [x] **Multi-agent handoff demo** (Researcher → Writer → Reviewer)
- [x] **Resumable-workflow demo** (crash mid-flight, resume from snapshot)
- [x] **Rate limiting** at the gateway (per-agent token bucket)
- [x] **Cost caps** at the gateway (1h + 24h rolling-window)
- [x] **Dashboard service** — single-page web UI for workspaces, audit, cost rollups
- [x] Python SDK at parity (channels + workflows + WorkflowHandle)
- [x] `.claude/launch.json` for `preview_start` integration
- [x] All five test suites green: 530 Python tests + 28 TS tests = 558 total

## v0.3 — Production-credible ✅ (released 2026-05-06)

**Goal**: production-safe for small-team paid use.

- [x] **Capability-token auth** — JWT (HS256) with scope grammar, issuance/verification/revocation
- [x] **Multi-tenancy** — `tenant_id` columns + tenant-scoped queries; isolated by JWT claims
- [x] **Identity service** (port 7425) with JWKS endpoint + tenant management
- [x] **Real GitHub OAuth flow** — PKCE-correct, AES-GCM at-rest encryption
- [x] **GitHub MCP server** (port 7426) — 7 real GitHub tools
- [x] **Issue-triage example** (Example 04)
- [x] **TypeScript SDK at parity** with Python — channels + workflows + identity, 79 tests
- [x] All five+ test suites green: 735 Python tests + 79 TS = **814 total**

## v0.4 — Scale & operability ✅ (released 2026-05-07)

**Goal**: handle real production traffic.

- [x] **Postgres backend option** (alongside SQLite) for workspace + gateway + identity
- [x] **OTLP-compatible event export** (gateway audit → OpenTelemetry Logs)
- [x] **Workspace garbage collection / retention policies** + admin sweep
- [x] **RS256 JWT with key rotation** (auto-rotate every 30d, JWKS published)
- [x] **Slack + Linear OAuth providers** + 2 new MCP servers
- [x] Dashboard time-series graph + OTLP status panel
- [x] All eight Python suites + TS suite green: **1084 tests** (996 Python + 88 TS)
- [ ] Federated revocation (deferred to v0.5)
- [ ] Stress-test benchmarks + load-shedding (deferred to v0.5)

## v0.5 — Reliability & coordination depth ✅ (released 2026-05-07)

**Goal**: durable workflow engine, atomic tool transactions, schema migrations.

- [x] **Migration framework** (versioned SQL + CLI + admin endpoints + checksums)
- [x] **Durable workflow executor** (worker pool with lease + heartbeat recovery)
- [x] **Workflow transactions** with Saga-style compensating actions
- [x] **Typed channels** + dead-letter queue + replay
- [x] **Stress benchmarks** + load-shedding middleware
- [x] **Demo 05** — durable workflow with crash recovery (verified end-to-end)
- [x] All eleven Python suites + TS suite green: **1373 tests** (1274 Python + 99 TS)
- [ ] Federated revocation (deferred to v0.6)
- [ ] Migration rollback execution (deferred to v0.6)
- [ ] Workflow visualisation in dashboard (deferred to v0.6)
- [ ] Lock/lease primitives for non-workflow resources (deferred to v0.6)

## v0.6 — Distribution & Polish ✅ (released 2026-05-07)

**Goal**: multi-node identity, richer dashboard, lock primitives, polish for first paying customers.

- [x] **Federated revocation** — Identity exposes `/v1/revocations`; Workspace + Gateway poll-based cache
- [x] **Postgres advisory locks** for migration runner across all three services
- [x] **Migration rollback execution** — CLI `--rollback-to`, dry-run, atomic per-step
- [x] **Generic resource lock/lease primitives** — workspace endpoints + Python/TS SDK + context manager
- [x] **Channel-schema migration helpers** — bulk check, replay-all DLQ, purge old; Dashboard buttons
- [x] **Workflow visualisation** in dashboard — graph view with status colors, click-for-detail, sortable list
- [ ] **TypeScript worker harness** (deferred to v0.7)

**1621 tests passing** (1503 Python + 118 TypeScript).

## v0.7 — Multi-tenant SaaS Posture (next)

**Goal**: turn Plinth from "self-hosted infra" into "ready for SaaS multi-tenant deployment".

- [ ] **TypeScript worker harness** (parity with Python worker)
- [ ] Per-tenant resource quotas (workspaces / channels / workflows / cost)
- [ ] Tenant-scoped admin UI in Dashboard (tenant CRUD + member management)
- [ ] Audit-log streaming via OTLP per tenant
- [ ] Channel-schema-evolution wizard in Dashboard (visual diff + migration plan)
- [ ] CLI tool (`plinth`) consolidating service ops + workflow control

## v0.6 — Observability deepening

**Goal**: every agent action is queryable, replayable, attributable.

- [ ] Unified semantic event stream (replacing per-tool audit log)
- [ ] Cost attribution per agent / workspace / customer
- [ ] Replay any past invocation
- [ ] Anomaly detection for agent behavior
- [ ] Time-series storage (ClickHouse / Tigris) for events at scale

## v0.7 — Enterprise auth & policy

**Goal**: features enterprises specifically need beyond v0.3 capability tokens.

- [ ] Capability-token issuance flow
- [ ] User-facing consent UI
- [ ] Policy engine (OPA-compatible)
- [ ] Approval workflows for sensitive operations
- [ ] SOC2-readiness checklist

## v1.0 — General availability

- [ ] Multi-region deployment story
- [ ] Cluster mode (no SPOFs)
- [ ] Enterprise SSO (SAML, OIDC)
- [ ] Compliance certifications path
- [ ] Stable API guarantees

## Themes always-on

- Documentation quality
- Example agent library (community contributions)
- Performance: p99 latency targets
- Cost: token efficiency benchmarks against alternatives
