# Plynf Roadmap

A living document. Reorder by community pull and demo data.

## v0.1 — PoC ✅ (released 2026-05-05)

**Goal**: prove the primitives. Show measurable agent token reduction.

- [x] Workspace service: KV + files + snapshots + branches, all versioned
- [x] Tool gateway: MCP proxy, caching, audit, mock auth
- [x] Python SDK
- [x] TypeScript SDK skeleton
- [x] Mock MCP server (6 tools, offline-capable)
- [x] Research-agent demo (baseline vs Plynf, 71 % token reduction measured)
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

## v1.0 — General Availability ✅ (released 2026-05-08)

**Goal**: stable API guarantees, production-ready ops, multi-region capable, compliance-aligned.

- [x] **Per-tenant resource quotas** (workspaces / storage / channels / workflows / tokens / OAuth / cost / RPM) with `QUOTA_EXCEEDED` enforcement
- [x] **Tenant Admin UI** in Dashboard — `/tenants` CRUD + per-tenant detail (members, quotas, OAuth, audit, cost)
- [x] **Channel Schema Evolution Wizard** — Dashboard modal for set/check/replay-all/purge
- [x] **Multi-region scaffolding** — region_id, peers, replica-mode 421 redirect with `X-Plynf-Primary-URL`, SDK auto-failover
- [x] **Unified `plinth` CLI** — services, migrate, workflow, audit, tenant, bench, health, completion (101 tests)
- [x] **GDPR data export** — async ZIP-bundled cross-service dump with token redaction
- [x] **GDPR data deletion** — two-phase cascade across Workspace + Gateway + Identity
- [x] **Tamper-evident audit chain** — SHA-256 prev_hash chain on `audit_events`, `/v1/audit/verify` endpoint
- [x] **Threat model** — STRIDE-based, 45 specific threats, 8 attacker classes
- [x] **API v1 stability promise** — additive-only guarantee, 12-month deprecation, contract tests
- [x] **Production deployment artifacts** — k8s manifests, Helm chart 1.0.0, Terraform AWS module, GHCR multi-arch release pipeline
- [x] **Comprehensive Prometheus metrics** — `/metrics` on every service with normalized paths
- [x] **Dashboard time-series graphs** — 24h + 7d trends for cost, latency p99, error rate, cache hit rate
- [x] **Formal SLOs** — 26 SLOs across services, burn-rate alerts, page-vs-ticket policy
- [x] All test suites green: **1901 Python + 136 TS-SDK + 29 TS-Worker = 2066 tests**

## v1.1 — Engineering-Debt Sweep + Strategic Adds ✅ (released 2026-05-10)

**Goal**: pay down engineering debt accumulated through GA + ship two strategic OAuth providers.

- [x] **OTel public-logs migration** — try-import wrapper + `<1.30` pin lifted
- [x] **Pluggable CoordinationBackend** (memory + Redis) — cluster-shared rate-limits + cost-caps + revocation
- [x] **Workflow retries with exponential backoff + jitter + DLQ** — `max_attempts`, `next_retry_at`, dead-letter queue endpoints
- [x] **Migration rollback files** for every existing migration — workspace + gateway + identity
- [x] **Lease-reaper jitter** — ±25% to prevent thundering herd
- [x] **Notion MCP server** (port 7429, 7 tools)
- [x] **Google Workspace MCP server** (port 7430, 8 tools — Drive, Docs, Sheets, Calendar, Gmail)
- [x] **CI hardening**: 12-suite Python matrix × 2 versions, Postgres service container, CodeQL, Dependabot, issue/PR templates, CODEOWNERS
- [x] **Real benchmark numbers** populated in README from `baseline-v1.1.json`
- [x] **2312 tests passing** (2139 Python + 144 TS-SDK + 29 TS-Worker)

## post-1.1 — Continuous improvement

**Goal**: harden, optimize, expand. v1.0 is stable; we iterate without breaking the API contract.

- [ ] **OTel SDK migration to public `logs` API** (currently pinned <1.30 due to internal API churn)
- [ ] **Federated revocation** of multi-node Identity (currently polling-based; Redis or gossip option)
- [ ] **Postgres advisory locks** for migration runner (currently fcntl flock; Postgres path needs work)
- [ ] **Migration rollback execution** for the rollback-to-target case (CLI exists, full rollback semantics next)
- [ ] **Cluster-mode workspace** — distributed lease coordinator beyond single-process locks
- [ ] **Enterprise SSO** — SAML + OIDC providers, group sync, just-in-time provisioning
- [ ] **Compliance certifications path** — SOC2 Type II audit prep, GDPR DPA template, HIPAA module
- [ ] **Workflow visualization v2** — historical replay, error attribution graphs
- [ ] **TS worker → Node distributed mode**, hot-reload handlers
- [ ] **More OAuth providers** — Notion, Jira, Confluence, Asana
- [ ] **Cost attribution at agent level** (today is at tenant level)
- [ ] **Anomaly detection** for agent behavior (token spikes, unusual tool patterns)

## Themes always-on

- Documentation quality
- Example agent library (community contributions)
- Performance: p99 latency targets
- Cost: token efficiency benchmarks against alternatives
