# Plinth — Technical Reference

| | |
|---|---|
| **Version** | v0.6 |
| **Date** | May 7, 2026 |
| **Commit** | `<commit-hash-placeholder>` |
| **License** | Apache 2.0 — see [`LICENSE`](./LICENSE) |
| **Source of truth** | [`CONTRACTS.md`](./CONTRACTS.md), [`docs/architecture/`](./docs/architecture/), [`docs/adr/`](./docs/adr/) |

This document is the holistic, end-to-end engineering reference for Plinth. It consolidates the architecture overview, the API contracts, the operational runbook, and the contribution playbook into a single, cross-linked document. A senior engineer should be able to deploy, debug, contribute to, and explain Plinth using only this file plus the source. Where this document and [`CONTRACTS.md`](./CONTRACTS.md) disagree, `CONTRACTS.md` wins.

---

## 1. System Overview

### 1.1 Architecture Diagram

```
                            ┌──────────────────────────────────┐
                            │            Your Agent            │
                            │   Python SDK   |  TypeScript SDK │
                            └──────────┬───────────────────────┘
                                       │  Authorization: Bearer <JWT>
            ┌──────────────────────────┼──────────────────────────┐
            │                          │                          │
   ┌────────▼─────────┐   ┌────────────▼────────┐   ┌─────────────▼──────────┐
   │  Workspace :7421 │   │   Tool Gateway :7422│   │   Identity :7425       │
   │                  │   │                     │   │                        │
   │ • KV (versioned) │   │ • Tool registry     │   │ • Issue JWT (HS|RS256) │
   │ • Files (blobs)  │   │ • MCP/HTTP proxy    │   │ • Verify / JWKS        │
   │ • Snapshots      │   │ • Cache (per-tool)  │   │ • Revoke + federate    │
   │ • Branches       │   │ • Audit log         │   │ • Tenants              │
   │ • Channels (DLQ) │   │ • Rate + cost caps  │   │ • Key rotation (30 d)  │
   │ • Workflows      │   │ • Transactions/Saga │   └────────────────────────┘
   │ • Locks/Leases   │   │ • OAuth (PKCE)      │
   │ • GC + Retention │   │ • OTLP export       │
   │ • Migrations     │   │ • Migrations        │
   └─────┬────────────┘   └────┬────────────┬───┘
         │                     │            │
   ┌─────▼─────┐    ┌──────────▼──┐  ┌──────▼──────────────────────────┐
   │ SQLite or │    │ Mock MCP    │  │  Real MCP servers               │
   │ Postgres  │    │ :7423       │  │  ┌─────────────────────────┐   │
   │ + blobs/  │    │  6 tools    │  │  │  GitHub  :7426  (7 tools) │   │
   └───────────┘    └─────────────┘  │  │  Slack   :7427  (4 tools) │   │
                                     │  │  Linear  :7428  (5 tools) │   │
                                     │  └─────────────────────────┘   │
                                     └─────────────────────────────────┘

                          ┌────────────────────────────────────┐
                          │  Dashboard :7424 — read-only SPA   │
                          │  Workspaces · Audit · Costs        │
                          │  OTLP status · Time-series graph   │
                          │  Workflow visualisation (v0.6)     │
                          │  DLQ inspector + replay buttons    │
                          └────────────────────────────────────┘

                          ┌────────────────────────────────────┐
                          │  plinth-workflow-worker (CLI)      │
                          │  Polls workspace, leases steps,    │
                          │  executes registered handlers,     │
                          │  heartbeats, drains on shutdown    │
                          └────────────────────────────────────┘
```

The agent loop and the LLM call live **outside** Plinth. Plinth owns the substrate: the persistent state the agent reads/writes, and the gateway that every external action passes through. See [`docs/architecture/01-system-overview.md`](./docs/architecture/01-system-overview.md) for the deeper walk-through.

### 1.2 Service Inventory

| Service | Port | Purpose | Language | Repo path | Tests | Status |
|---|---:|---|---|---|---:|---|
| **Workspace** | 7421 | Versioned KV + files + snapshots + branches + channels + workflows + locks + GC + tenants | Python 3.11 / FastAPI | [`services/workspace/`](./services/workspace/) | 465 | Stable |
| **Gateway** | 7422 | MCP proxy + cache + audit + rate-limits + cost caps + OAuth + transactions + OTLP | Python 3.11 / FastAPI | [`services/gateway/`](./services/gateway/) | 396 | Stable |
| **Identity** | 7425 | JWT capability tokens + tenants + JWKS + RS256 key rotation + federated revocation | Python 3.11 / FastAPI | [`services/identity/`](./services/identity/) | 166 | Stable |
| **Dashboard** | 7424 | Read-only SPA + proxies + time-series graph + workflow viz | Python 3.11 / FastAPI + vanilla JS | [`services/dashboard/`](./services/dashboard/) | 60 | Stable |
| **Mock MCP** | 7423 | 6 demo tools (`web.fetch`, `web.search`, `fs.read`, `fs.write`, `notes.add`, `notes.list`) | Python 3.11 / FastAPI | [`mock-mcp-server/`](./mock-mcp-server/) | ~33 | Stable |
| **GitHub MCP** | 7426 | 7 tools, real GitHub REST API | Python 3.11 / FastAPI | [`mcp-servers/github/`](./mcp-servers/github/) | 42 | Stable |
| **Slack MCP** | 7427 | 4 tools, real Slack Web API | Python 3.11 / FastAPI | [`mcp-servers/slack/`](./mcp-servers/slack/) | 23 | Stable |
| **Linear MCP** | 7428 | 5 tools, real Linear GraphQL API | Python 3.11 / FastAPI | [`mcp-servers/linear/`](./mcp-servers/linear/) | 27 | Stable |
| Python SDK | — | Full client surface | Python 3.11 | [`sdk/python/`](./sdk/python/) | 267 | Stable |
| TypeScript SDK | — | Full parity client surface | TypeScript 5.4+ / ESM | [`sdk/typescript/`](./sdk/typescript/) | 118 | Stable |
| Worker CLI | — | `plinth-workflow-worker` durable-execution daemon | Python 3.11 | [`worker/`](./worker/) | 13 | Stable |
| Benchmarks | — | `plinth-bench` open-loop load harness | Python 3.11 | [`benchmarks/`](./benchmarks/) | 11 | Stable |

**Total: 1503 Python tests + 118 TypeScript tests = 1621 tests passing.** 15 Postgres-only tests skipped unless `PLINTH_TEST_POSTGRES_URL` is set. See the per-version breakdown in [`CHANGELOG.md`](./CHANGELOG.md).

### 1.3 Request Flow — One Tool Invocation, End to End

This is the canonical shape of every gateway call. Source: [`docs/architecture/01-system-overview.md`](./docs/architecture/01-system-overview.md) §3 and [`docs/architecture/03-tool-gateway-design.md`](./docs/architecture/03-tool-gateway-design.md) §2.

```
Agent ──▶ SDK ──▶ POST /v1/invoke {tool_id, args, workspace_id, agent_id}
                       │
                       ▼
                ┌──────────────┐
                │ Auth check   │ extract bearer ▸ verify JWT (local or remote)
                │              │ check revocation cache (v0.6)
                └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ Capability   │ scope grammar: tool:<id>, workspace:<id>:write, …
                │ check        │
                └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ Rate / cost  │ token bucket per agent_id;
                │ enforce      │ rolling 1h + 24h $ ceilings ▸ 429 if exceeded
                └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ Cache lookup │ key = sha256(tool_id ‖ canonical_json(args))
                │              │ HIT (idempotent + within TTL) ▸ return cached
                └──────┬───────┘ MISS
                       ▼
                ┌──────────────┐
                │ Auth backend │ resolve bearer / OAuth connection / API key
                └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ Proxy        │ HTTP/JSON to MCP server (mock-mcp / github-mcp / …)
                │              │ apply per-tool timeout + provider-specific headers
                └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ Audit append │ INSERT audit_events (synchronous, in-band)
                │              │ ALSO emit OTLP log if PLINTH_OTLP_ENABLED=true
                └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ Cache store  │ if idempotent + 2xx and TTL > 0
                └──────┬───────┘
                       ▼
                  InvokeResponse {result, cached, duration_ms, audit_id, cost_estimate_usd}
```

Three load-bearing properties:

1. **Audit is synchronous, never async.** The audit row is written before the response is returned. We trade ~2-5 ms latency for the guarantee that no invocation is unrecorded.
2. **The cache key is the hash of `(tool_id, canonical_json(args))`.** Argument order, whitespace and number representation are normalised before hashing — see `services/gateway/src/plinth_gateway/cache.py`.
3. **`workspace_id` and `agent_id` on `/v1/invoke` are metadata only.** They do not authorize the call (that's the JWT's job); they are recorded in the audit row so cost-attribution queries are O(1) SQL.

### 1.4 Data Flow — One KV Write with Versioning + Branch

This is the canonical shape of every workspace state mutation. Source: [`docs/architecture/02-workspace-design.md`](./docs/architecture/02-workspace-design.md) §2-3.

```
Agent ──▶ ws.with_branch(br_a).kv.set("topic", "wind+solar")
            │
            ▼
        SDK encodes path: PUT /v1/workspaces/{ws}/kv/topic?branch=br_a
            │
            ▼
   ┌────────────────────────────────────────────────────────────┐
   │ Workspace service                                          │
   │                                                            │
   │  1. Auth: verify JWT, check workspace:{ws}:write scope    │
   │  2. Branch validation: br_a ∈ workspace, not merged       │
   │  3. Compute next version:                                  │
   │       SELECT COALESCE(MAX(version), 0) + 1                 │
   │         FROM kv_entries                                    │
   │         WHERE workspace_id=? AND key=? AND branch_id=?    │
   │  4. INSERT new row (immutable):                            │
   │       (workspace_id, "topic", N, value_json,               │
   │        deleted=0, branch_id="br_a", created_at)            │
   │  5. Tenant filter: tenant_id pinned from JWT              │
   │  6. Return 200 KVEntry {version: N}                        │
   └────────────────────────────────────────────────────────────┘

  Subsequent reads:
    GET /v1/workspaces/{ws}/kv/topic?branch=br_a
       1. Check branch's own writes  ▸ HIT  ▸ return branch row N
       2. Else fall through to br_a.from_snapshot.kv_versions["topic"]
       3. Else 404 KEY_NOT_FOUND

  Snapshotting:
    POST /v1/workspaces/{ws}/snapshots {name, message?}
       SELECT MAX(version) GROUP BY key, path on the targeted timeline
       INSERT snapshot row with kv_versions_json + file_versions_json
       ▸ Snapshots are O(unique-keys) metadata, not data copies.
```

A KV write is therefore one INSERT — never an UPDATE. Tombstones (`DELETE`) are also INSERTs with `deleted=1`. This is what makes [snapshots O(unique-keys)](./docs/architecture/02-workspace-design.md#4-snapshots-lazy-by-design) and history append-only.

---

## 2. Installation & Setup

### 2.1 Prerequisites

| Component | Version | Required for | Install hint |
|---|---|---|---|
| Python | 3.11+ | All services + Python SDK | `brew install python@3.11` / `apt install python3.11` |
| Node | 20+ | TypeScript SDK + tests | `brew install node@20` / nvm |
| GNU Make | any | Top-level orchestration | usually pre-installed |
| `git` | any | clone / contribute | usually pre-installed |
| Docker | 24+ | Optional — container stack via `docker-compose.yml` | docker.com |
| Postgres | 15+ | Optional — production storage backend | `brew install postgresql@15` |
| OTLP collector | any | Optional — observability sink | OpenTelemetry Collector or Datadog/Tempo/Honeycomb |

Plinth runs offline by default: Mock MCP fixtures live in [`mock-mcp-server/`](./mock-mcp-server/), and OAuth flows have a simulation mode for the GitHub demo.

### 2.2 First-Time Install

```bash
git clone https://github.com/nico-schindlbeck-jpg/plinth.git
cd plinth
make install
```

`make install` (defined in [`Makefile`](./Makefile) lines 50-126) creates `.venv/`, installs all four services, the three real MCP servers, the mock MCP server, the dashboard, the identity service, the Python SDK, and all five examples in editable mode (`pip install -e`). Total disk footprint: ~250 MB.

### 2.3 Verifying Install — `make test`

```bash
make test
```

Runs every Python suite via `pytest -q --cov=...`. Expected counts (from [`CHANGELOG.md`](./CHANGELOG.md) §0.6.0):

| Suite | Count | Coverage target |
|---|---:|---:|
| `services/workspace` | 465 | ≥ 90 % |
| `services/gateway` | 396 | ≥ 90 % |
| `services/identity` | 166 | ≥ 90 % |
| `services/dashboard` | 60 | ≥ 80 % |
| `mock-mcp-server` | ~33 | ≥ 95 % |
| `mcp-servers/github` | 42 | ≥ 90 % |
| `mcp-servers/slack` | 23 | ≥ 90 % |
| `mcp-servers/linear` | 27 | ≥ 90 % |
| `sdk/python` | 267 | ≥ 90 % |
| `worker` | 13 | ≥ 80 % |
| `benchmarks` | 11 | ≥ 80 % |
| **Python total** | **1503** | |
| `sdk/typescript` (run via `make test-ts`) | 118 | ≥ 80 % |
| **Grand total** | **1621** | |

15 Postgres-only tests skip without `PLINTH_TEST_POSTGRES_URL` set; see [§12.3](#123-postgres-backend-setup). For the TypeScript suite:

```bash
make test-ts            # cd sdk/typescript && npm install && npm run build && npm test
```

### 2.4 Running Services Locally — `make serve`

```bash
make serve              # spawns 8 background processes
make stop               # SIGTERMs all of them
```

`make serve` (Makefile §218-247) shell-spawns each service via `scripts/_spawn.py` — pid files at `/tmp/plinth-pids/`, logs at `/tmp/plinth-logs/`. Default ports:

| Service | URL |
|---|---|
| Workspace | http://localhost:7421/healthz |
| Gateway | http://localhost:7422/healthz |
| Mock MCP | http://localhost:7423/healthz |
| Dashboard | http://localhost:7424/ |
| Identity | http://localhost:7425/healthz |
| GitHub MCP | http://localhost:7426/healthz |
| Slack MCP | http://localhost:7427/healthz |
| Linear MCP | http://localhost:7428/healthz |

Override any port via `PLINTH_*_PORT` env vars listed in the Makefile lines 14-22.

### 2.5 Running with Docker Compose

```bash
docker compose up --build           # foreground
docker compose up -d                # background
docker compose logs -f workspace
docker compose down -v              # also wipes the data volume
```

The [`docker-compose.yml`](./docker-compose.yml) file packages all 8 services with a shared `plinth_data` volume mounted at `/data`, healthchecks on `/healthz`, and `restart: unless-stopped`. The dashboard and gateway depend on a healthy workspace before starting (`depends_on.condition: service_healthy`).

### 2.6 Running with Postgres Backend

```bash
# 1. Start Postgres
brew services start postgresql@15
createdb plinth

# 2. Switch driver per service (or globally)
export PLINTH_STORAGE_DRIVER=postgres
export PLINTH_DATABASE_URL=postgresql://localhost:5432/plinth

# 3. Apply migrations (workspace shown — repeat for gateway + identity)
.venv/bin/python -m plinth_workspace migrate

# 4. Start services
make serve
```

The migration runner (`MigrationRunner` in `services/<svc>/src/plinth_<svc>/migration_runner.py`) auto-applies on startup when `PLINTH_AUTO_MIGRATE=true` (the default since v0.5). On Postgres, multiple replicas can boot concurrently because the runner uses `pg_advisory_lock(<service-hash>)` instead of `fcntl.flock` — see [`CHANGELOG.md`](./CHANGELOG.md) v0.6 entry and [`CONTRACTS.md`](./CONTRACTS.md) §"Postgres Advisory Locks".

Per-service overrides:

```bash
export PLINTH_WORKSPACE_DATABASE_URL=postgresql://wsuser:pw@host/plinth_ws
export PLINTH_GATEWAY_DATABASE_URL=postgresql://gwuser:pw@host/plinth_gw
export PLINTH_IDENTITY_DATABASE_URL=postgresql://iduser:pw@host/plinth_id
export PLINTH_DB_POOL_MIN_SIZE=5
export PLINTH_DB_POOL_MAX_SIZE=20
```

### 2.7 Running with OpenTelemetry Export

```bash
# Run any OTLP/HTTP collector on :4318 (otelcol, Datadog Agent, Tempo, …)
export PLINTH_OTLP_ENABLED=true
export PLINTH_OTLP_ENDPOINT=http://localhost:4318
export PLINTH_OTLP_SERVICE_NAME=plinth-gateway
export PLINTH_OTLP_BATCH_SIZE=64
export PLINTH_OTLP_FLUSH_INTERVAL_SECONDS=2.0
export PLINTH_OTLP_HEADERS_JSON='{"Authorization": "Bearer …"}'

make serve
curl http://localhost:7422/v1/observability/status   # confirm enabled, events emitted
```

Contract: [`CONTRACTS.md`](./CONTRACTS.md) §"OTLP Event Stream". Spec: [`specs/schemas/event.schema.json`](./specs/schemas/event.schema.json). Failure mode: emit failures never crash the gateway — the buffer is dropped if the collector is unreachable for `2 × FLUSH_INTERVAL`.

### 2.8 Health Checks

```bash
make healthcheck                         # script at scripts/healthcheck.sh

# Or curl manually:
curl http://localhost:7421/healthz       # → {"status":"ok","version":"0.6.0","service":"workspace"}
curl http://localhost:7422/healthz
curl http://localhost:7425/healthz
# … for every service
```

Every service exposes `GET /healthz` returning `{"status":"ok","version":"<svc>.<v>","service":"<svc>"}`. Docker Compose healthchecks run this every 10 s with a 3 s timeout and 5 retries.

### 2.9 Stopping Services + Cleanup

```bash
make stop                # SIGTERMs every running service (uses pid files)
make clean-data          # ALSO wipes /tmp/plinth-data, /tmp/plinth-logs, /tmp/plinth-pids
make clean               # removes .venv and build artifacts (does NOT touch data)
```

`make clean-data` calls `make stop` first (Makefile line 426); safe to run with stale services.

---

## 3. Service Reference

### 3.1 Workspace Service

#### 3.1.1 Purpose

Persistent, versioned, structured memory for agents: a key-value store with monotonic-int versioning per (workspace, key); a content-addressed file plane keyed by SHA-256; immutable snapshots; divergent branches; durable channels with optional JSON Schema enforcement and dead-letter queues; checkpointed workflows with worker-leasable steps; generic resource locks; per-workspace garbage collection with retention policies. The workspace owns no inbound state from the gateway — both services share the data dir, but no in-process state.

#### 3.1.2 Port and URL

`http://localhost:7421` by default. Override via `PLINTH_WORKSPACE_PORT`.

#### 3.1.3 Configuration

All configuration is read from environment variables prefixed with `PLINTH_`. Source: `services/workspace/src/plinth_workspace/settings.py`.

| Variable | Type | Default | Meaning |
|---|---|---|---|
| `PLINTH_DATA_DIR` | path | `/tmp/plinth-data` | Where SQLite + blobs live |
| `PLINTH_WORKSPACE_PORT` | int | `7421` | Bind port |
| `PLINTH_LOG_LEVEL` | enum | `INFO` | DEBUG/INFO/WARNING/ERROR |
| `PLINTH_LOG_FORMAT` | enum | `console` | `console` or `json` |
| `PLINTH_STORAGE_DRIVER` | enum | `sqlite` | `sqlite` or `postgres` |
| `PLINTH_DATABASE_URL` | URL | — | required when driver=postgres (asyncpg DSN) |
| `PLINTH_WORKSPACE_DATABASE_URL` | URL | — | per-service Postgres override |
| `PLINTH_DB_POOL_MIN_SIZE` | int | `5` | Postgres pool floor |
| `PLINTH_DB_POOL_MAX_SIZE` | int | `20` | Postgres pool ceiling |
| `PLINTH_AUTO_MIGRATE` | bool | `true` | Run migrations on startup |
| `PLINTH_AUTH_REQUIRED` | bool | `false` | Reject unauthenticated requests |
| `PLINTH_AUTH_MODE` | enum | `permissive` | `permissive` / `verify_local` / `verify_remote` |
| `PLINTH_IDENTITY_JWT_SECRET` | bytes | — | HS256 shared secret |
| `PLINTH_IDENTITY_URL` | URL | — | for `verify_remote` and JWKS fetch |
| `PLINTH_LEASE_REAPER_ENABLED` | bool | `true` | Sweep expired step-leases |
| `PLINTH_LEASE_REAPER_INTERVAL_SECONDS` | int | `30` | Reaper cadence |
| `PLINTH_WORKER_INACTIVE_TIMEOUT_SECONDS` | int | `300` | Mark workers `gone` after no heartbeat |
| `PLINTH_LOAD_SHED_ENABLED` | bool | `false` | Enable bounded-inflight middleware |
| `PLINTH_LOAD_SHED_MAX_INFLIGHT` | int | `200` | Concurrent requests before 503 |
| `PLINTH_LOAD_SHED_MAX_QUEUE` | int | `1000` | Pending queue before 503 |
| `PLINTH_LOAD_SHED_RETRY_AFTER_SECONDS` | int | `1` | `Retry-After` header on 503 |
| `PLINTH_REVOCATION_POLL_URL` | URL | `""` | Identity URL for revocation cache; empty disables polling |
| `PLINTH_REVOCATION_POLL_INTERVAL_SECONDS` | int | `60` | Revocation cache refresh cadence |
| `PLINTH_REVOCATION_POLL_ENABLED` | bool | `true` | Toggle polling globally |

#### 3.1.4 Endpoints

Full schemas: [`specs/openapi/workspace.yaml`](./specs/openapi/workspace.yaml). Prose source of truth: [`CONTRACTS.md`](./CONTRACTS.md) §Workspace API.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Liveness probe |
| `POST` | `/v1/workspaces` | Create workspace |
| `GET` | `/v1/workspaces` | List workspaces (tenant-filtered) |
| `GET` | `/v1/workspaces/{ws_id}` | Get workspace |
| `DELETE` | `/v1/workspaces/{ws_id}` | Delete workspace |
| `PUT` | `/v1/workspaces/{ws_id}/kv/{key:path}` | Versioned KV write |
| `GET` | `/v1/workspaces/{ws_id}/kv/{key:path}` | KV read (`?version=`, `?branch=`) |
| `DELETE` | `/v1/workspaces/{ws_id}/kv/{key:path}` | KV tombstone |
| `GET` | `/v1/workspaces/{ws_id}/kv/{key:path}/history` | All versions of a key |
| `GET` | `/v1/workspaces/{ws_id}/kv` | All latest KV entries |
| `PUT` | `/v1/workspaces/{ws_id}/files/{path:path}` | Versioned file write (raw bytes) |
| `GET` | `/v1/workspaces/{ws_id}/files/{path:path}` | File read (`?version=`, `?branch=`) |
| `GET` | `/v1/workspaces/{ws_id}/files/{path:path}/meta` | File metadata |
| `DELETE` | `/v1/workspaces/{ws_id}/files/{path:path}` | File tombstone |
| `GET` | `/v1/workspaces/{ws_id}/files` | File listing |
| `POST` | `/v1/workspaces/{ws_id}/snapshots` | Capture snapshot |
| `GET` | `/v1/workspaces/{ws_id}/snapshots` | List snapshots |
| `GET` | `/v1/workspaces/{ws_id}/snapshots/{snap_id}` | Get snapshot |
| `GET` | `/v1/workspaces/{ws_id}/snapshots/{snap_id}/diff` | Diff vs `?against=` |
| `POST` | `/v1/workspaces/{ws_id}/branches` | Branch from snapshot |
| `GET` | `/v1/workspaces/{ws_id}/branches` | List branches |
| `POST` | `/v1/workspaces/{ws_id}/branches/{branch_id}/merge` | Merge branch into main |
| `DELETE` | `/v1/workspaces/{ws_id}/branches/{branch_id}` | Delete branch |
| `POST` | `/v1/workspaces/{ws_id}/channels/{name:path}/send` | Channel send |
| `GET` | `/v1/workspaces/{ws_id}/channels/{name:path}/receive` | Channel receive (cursor) |
| `DELETE` | `/v1/workspaces/{ws_id}/channels/{name:path}/messages/{message_id}` | Ack/delete |
| `GET` | `/v1/workspaces/{ws_id}/channels` | List channels |
| `POST` | `/v1/workspaces/{ws_id}/channels/{name:path}/schema` | Attach JSON Schema |
| `DELETE` | `/v1/workspaces/{ws_id}/channels/{name:path}/schema` | Drop schema |
| `GET` | `/v1/workspaces/{ws_id}/channels/{name:path}/deadletter` | DLQ contents |
| `POST` | `/v1/workspaces/{ws_id}/channels/{name:path}/deadletter/{msg_id}/replay` | Single replay |
| `POST` | `/v1/workspaces/{ws_id}/channels/{name:path}/deadletter/replay-all` | Bulk replay (`?dry_run=`) |
| `DELETE` | `/v1/workspaces/{ws_id}/channels/{name:path}/deadletter` | Purge DLQ (`?older_than_seconds=`) |
| `POST` | `/v1/workspaces/{ws_id}/channels/{name:path}/schema/check` | Validate sample against new schema |
| `POST` | `/v1/workspaces/{ws_id}/workflows` | Create workflow |
| `GET` | `/v1/workspaces/{ws_id}/workflows` | List workflows |
| `GET` | `/v1/workspaces/{ws_id}/workflows/{wf_id}` | Workflow + steps |
| `POST` | `/v1/workspaces/{ws_id}/workflows/{wf_id}/steps` | Start step |
| `PATCH` | `/v1/workspaces/{ws_id}/workflows/{wf_id}/steps/{step_id}` | Complete/fail/cancel |
| `GET` | `/v1/workspaces/{ws_id}/workflows/{wf_id}/resume` | Resume info |
| `POST` | `/v1/workspaces/{ws_id}/workflows/{wf_id}/cancel` | Cancel workflow |
| `POST` | `/v1/workspaces/{ws_id}/workflows/{wf_id}/steps/{step_id}/lease` | Acquire step lease |
| `POST` | `/v1/workspaces/{ws_id}/workflows/{wf_id}/steps/{step_id}/heartbeat` | Heartbeat lease |
| `POST` | `/v1/workspaces/{ws_id}/workflows/{wf_id}/steps/{step_id}/release` | Release lease |
| `GET` | `/v1/workspaces/{ws_id}/workflows/{wf_id}/pending` | Pending steps |
| `GET` | `/v1/workspaces/{ws_id}/workflows/{wf_id}/expired` | Expired leases |
| `POST` | `/v1/workers/register` | Worker registration |
| `POST` | `/v1/workers/{worker_id}/heartbeat` | Worker heartbeat |
| `POST` | `/v1/workers/{worker_id}/drain` | Graceful shutdown |
| `GET` | `/v1/workers` | Worker listing (`?status=`) |
| `POST` | `/v1/workspaces/{ws_id}/locks/{name:path}/acquire` | Generic resource lock acquire |
| `POST` | `/v1/workspaces/{ws_id}/locks/{name:path}/heartbeat` | Lock heartbeat |
| `POST` | `/v1/workspaces/{ws_id}/locks/{name:path}/release` | Lock release |
| `GET` | `/v1/workspaces/{ws_id}/locks` | Lock listing |
| `GET` | `/v1/workspaces/{ws_id}/locks/{name:path}` | Lock get |
| `POST` | `/v1/workspaces/{ws_id}/gc` | Per-workspace GC sweep |
| `GET` | `/v1/workspaces/{ws_id}/retention` | Get retention policy |
| `PUT` | `/v1/workspaces/{ws_id}/retention` | Set retention policy |
| `POST` | `/v1/admin/gc` | Admin sweep across all policies |
| `GET` | `/v1/admin/migrations` | Migration status |
| `POST` | `/v1/admin/migrations/apply` | Apply pending |
| `POST` | `/v1/admin/migrations/rollback` | Rollback to a target id |
| `GET` | `/v1/tenants` | Tenant listing |

#### 3.1.5 Storage

| Table | Owner | What it holds |
|---|---|---|
| `workspaces` | workspace | One row per workspace, `tenant_id` column for multi-tenancy |
| `kv_entries` | workspace | One row per (workspace, key, version, branch_id?) |
| `file_entries` | workspace | One row per (workspace, path, version, branch_id?), references blob by SHA-256 |
| `snapshots` | workspace | `kv_versions_json` + `file_versions_json` snapshots |
| `branches` | workspace | One row per branch with `from_snapshot_id` |
| `channels` | workspace | Channel metadata (lazily created on first send) |
| `channel_messages` | workspace | One row per message, `seq` monotonic per channel |
| `channel_schemas` | workspace | Optional JSON Schema attached per channel |
| `workflows` | workspace | Manifest + metadata |
| `workflow_steps` | workspace | One row per (workflow, step_name, attempt) |
| `workflow_step_leases` | workspace | Per-step lease with `worker_id`, `expires_at` |
| `workers` | workspace | Worker registry with last heartbeat |
| `resource_locks` | workspace | Generic locks |
| `retention_policies` | workspace | Per-workspace GC rules |
| `schema_migrations` | workspace | Applied migration ids + sha256 checksums |
| Blobs | workspace | `$PLINTH_DATA_DIR/blobs/<sha256>` content-addressed files |

Schema definitions are inline in [`docs/architecture/02-workspace-design.md`](./docs/architecture/02-workspace-design.md), [`docs/architecture/07-channels-design.md`](./docs/architecture/07-channels-design.md) and [`docs/architecture/08-workflows-design.md`](./docs/architecture/08-workflows-design.md).

#### 3.1.6 Dependencies

The workspace service is intentionally a **leaf** in the service graph: it talks to nothing else inbound. It optionally polls the Identity service for the revocation cache (when `PLINTH_REVOCATION_POLL_URL` is set) and reads JWKS from Identity for RS256 verification.

#### 3.1.7 Test Suite

465 tests in `services/workspace/tests/` (counts from [`CHANGELOG.md`](./CHANGELOG.md) v0.6.0). Coverage: workspace lifecycle, KV versioning, branch fall-through, snapshot diff, channel ordering, channel schemas + DLQ replay, workflow state machine, lease acquisition/expiry, retention GC, generic locks, migration framework + rollback, revocation cache, load-shedding, Postgres parity (skipped without `PLINTH_TEST_POSTGRES_URL`).

### 3.2 Gateway Service

#### 3.2.1 Purpose

The single boundary every external tool call passes through. Caches by canonical-arg-hash, audits every invocation synchronously, enforces per-agent rate limits and rolling-window cost ceilings, runs OAuth Authorization-Code-with-PKCE flows on behalf of agents, brokers JWT capability tokens, exports OTLP logs, and groups multiple invocations into Saga-style transactions with compensating actions.

#### 3.2.2 Port and URL

`http://localhost:7422` by default. Override via `PLINTH_GATEWAY_PORT`.

#### 3.2.3 Configuration

| Variable | Type | Default | Meaning |
|---|---|---|---|
| `PLINTH_DATA_DIR` | path | `/tmp/plinth-data` | Where `gateway.db` lives |
| `PLINTH_GATEWAY_PORT` | int | `7422` | Bind port |
| `PLINTH_STORAGE_DRIVER` | enum | `sqlite` | `sqlite` or `postgres` |
| `PLINTH_DATABASE_URL` / `PLINTH_GATEWAY_DATABASE_URL` | URL | — | Postgres DSN |
| `PLINTH_AUTO_MIGRATE` | bool | `true` | Auto-apply on startup |
| `PLINTH_AUTH_MODE` | enum | `permissive` | `permissive` / `verify_local` / `verify_remote` |
| `PLINTH_IDENTITY_JWT_SECRET` | bytes | — | HS256 secret |
| `PLINTH_IDENTITY_URL` | URL | — | for `verify_remote` and JWKS fetch |
| `PLINTH_RATE_LIMITS_ENABLED` | bool | `true` | Disable globally |
| `PLINTH_RATE_LIMIT_DEFAULT_RPM` | int | `60` | Token bucket refill rate |
| `PLINTH_RATE_LIMIT_DEFAULT_BURST` | int | `20` | Token bucket capacity |
| `PLINTH_COST_CAP_DEFAULT_USD_HOUR` | float | `1.0` | Hourly $ ceiling |
| `PLINTH_COST_CAP_DEFAULT_USD_DAY` | float | `10.0` | Daily $ ceiling |
| `PLINTH_OAUTH_GITHUB_CLIENT_ID` | str | — | GitHub OAuth client |
| `PLINTH_OAUTH_GITHUB_CLIENT_SECRET` | str | — | GitHub OAuth secret |
| `PLINTH_OAUTH_GITHUB_REDIRECT_URI` | URL | `http://localhost:7422/v1/oauth/github/callback` | OAuth callback |
| `PLINTH_OAUTH_GITHUB_SCOPES` | str | `repo,read:user` | Comma-separated scopes |
| `PLINTH_OAUTH_SLACK_*` | various | — | Slack provider |
| `PLINTH_OAUTH_LINEAR_*` | various | — | Linear provider |
| `PLINTH_OAUTH_ENCRYPTION_KEY` | base64 | auto-gen dev key + warning | AES-256-GCM at-rest token key |
| `PLINTH_OTLP_ENABLED` | bool | `false` | Emit OTLP logs |
| `PLINTH_OTLP_ENDPOINT` | URL | — | OTLP/HTTP collector |
| `PLINTH_OTLP_SERVICE_NAME` | str | `plinth-gateway` | `service.name` resource attr |
| `PLINTH_OTLP_BATCH_SIZE` | int | `64` | Batch flush threshold |
| `PLINTH_OTLP_FLUSH_INTERVAL_SECONDS` | float | `2.0` | Periodic flush cadence |
| `PLINTH_OTLP_HEADERS_JSON` | JSON | `{}` | Auth headers for the collector |
| `PLINTH_LOAD_SHED_ENABLED` | bool | `false` | Bounded inflight middleware |
| `PLINTH_LOAD_SHED_MAX_INFLIGHT` | int | `200` | |
| `PLINTH_LOAD_SHED_MAX_QUEUE` | int | `1000` | |
| `PLINTH_LOAD_SHED_RETRY_AFTER_SECONDS` | int | `1` | |
| `PLINTH_REVOCATION_POLL_URL` | URL | `""` | Identity URL for revocation cache |
| `PLINTH_REVOCATION_POLL_INTERVAL_SECONDS` | int | `60` | Revocation refresh |

#### 3.2.4 Endpoints

Spec: [`specs/openapi/gateway.yaml`](./specs/openapi/gateway.yaml). Prose: [`CONTRACTS.md`](./CONTRACTS.md) §Gateway API.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Liveness |
| `POST` | `/v1/tools/register` | Register tool |
| `GET` | `/v1/tools` | List tools |
| `GET` | `/v1/tools/{tool_id}` | Get tool |
| `DELETE` | `/v1/tools/{tool_id}` | Deregister |
| `POST` | `/v1/invoke` | Invoke a tool |
| `POST` | `/v1/invoke/dry-run` | Cost/cache preview, no side effects |
| `GET` | `/v1/audit` | Audit query (`?workspace_id=&tool_id=&since=&limit=`) |
| `GET` | `/v1/audit/stats` | Audit aggregates |
| `GET` | `/v1/cache/stats` | Cache hit/miss/size |
| `DELETE` | `/v1/cache` | Cache clear (`?tool_id=`) |
| `POST` | `/v1/limits/{agent_id}` | Set per-agent limits |
| `GET` | `/v1/limits/{agent_id}` | Get limits |
| `GET` | `/v1/limits/{agent_id}/status` | Current usage |
| `DELETE` | `/v1/limits/{agent_id}` | Reset to defaults |
| `GET` | `/v1/oauth/{provider}/authorize` | 302 redirect to provider with PKCE |
| `GET` | `/v1/oauth/{provider}/callback` | 302 back with code → connection |
| `POST` | `/v1/oauth/{provider}/refresh` | Refresh access token |
| `POST` | `/v1/oauth/connections` | Create connection |
| `GET` | `/v1/oauth/connections` | List (`?tenant_id=`) |
| `GET` | `/v1/oauth/connections/{conn_id}` | Get connection (no token) |
| `DELETE` | `/v1/oauth/connections/{conn_id}` | Revoke locally + best-effort at provider |
| `POST` | `/v1/transactions` | Create transaction |
| `POST` | `/v1/transactions/{tx_id}/calls` | Add call |
| `POST` | `/v1/transactions/{tx_id}/commit` | Execute (with compensation cascade) |
| `POST` | `/v1/transactions/{tx_id}/rollback` | Compensate without committing |
| `GET` | `/v1/transactions/{tx_id}` | Get transaction |
| `GET` | `/v1/observability/status` | OTLP status |
| `POST` | `/v1/observability/flush` | Force OTLP flush (admin) |
| `GET` | `/v1/admin/migrations` | Migration status |
| `POST` | `/v1/admin/migrations/apply` | Apply pending |
| `POST` | `/v1/admin/migrations/rollback` | Rollback target |
| `GET` | `/v1/tenants` | Tenant listing |

#### 3.2.5 Storage

| Table | Holds |
|---|---|
| `tools` | Registered tools with `cache_ttl_seconds`, `idempotent`, `auth_*` |
| `audit_events` | Append-only invocation log |
| `cache_entries` | `(key, result_json, created_at, expires_at, tool_id)` |
| `agent_limits` | Per-agent rpm/burst/cost overrides |
| `oauth_connections` | Encrypted access + refresh tokens, by provider+user |
| `oauth_state` | TTL'd CSRF/PKCE state with replay protection |
| `transactions`, `transaction_calls` | Saga ledger |
| `schema_migrations` | Migration tracking |

#### 3.2.6 Dependencies

- **Workspace service** (read): the gateway records `workspace_id` in audit but never writes to the workspace.
- **Identity service** (optional): JWKS fetch (5-min cache) when `PLINTH_AUTH_MODE=verify_local` with RS256; revocation polling when `PLINTH_REVOCATION_POLL_URL` is set.
- **Real MCP servers / OAuth providers** (outbound): every `/v1/invoke` proxies to the configured tool endpoint.
- **OTLP collector** (optional, outbound): when `PLINTH_OTLP_ENABLED=true`.

#### 3.2.7 Test Suite

396 tests in `services/gateway/tests/`. Coverage: tool registry, cache canonicalisation + TTL, audit append-only, rate-limit token bucket, cost cap rolling windows, OAuth Authorization-Code-with-PKCE for all 3 providers, AES-GCM token encryption, transaction commit + compensation cascade, OTLP buffer + retry, RS256 JWKS verifier, migration framework + rollback, advisory-lock unit tests, revocation cache.

### 3.3 Identity Service

#### 3.3.1 Purpose

Issues, verifies, and revokes JWT capability tokens (HS256 or RS256). Manages tenants. Publishes JWKS for federated public-key verification by Workspace and Gateway. Rotates RS256 keys every 30 days (default) with overlap so previously-issued tokens still verify until they themselves expire. Exposes the federated revocation cursor endpoint that Workspace and Gateway poll.

#### 3.3.2 Port and URL

`http://localhost:7425` by default. Override via `PLINTH_IDENTITY_PORT`.

#### 3.3.3 Configuration

| Variable | Type | Default | Meaning |
|---|---|---|---|
| `PLINTH_IDENTITY_PORT` | int | `7425` | Bind port |
| `PLINTH_IDENTITY_DATA_DIR` | path | `$PLINTH_DATA_DIR` | Where `identity.db` + keys live |
| `PLINTH_IDENTITY_JWT_SECRET` | base64 | auto-gen dev key + warning | HS256 secret |
| `PLINTH_IDENTITY_JWT_ALG` | enum | `HS256` | `HS256` or `RS256` |
| `PLINTH_IDENTITY_ISSUER` | URL | `http://localhost:7425` | `iss` claim |
| `PLINTH_IDENTITY_AUDIENCE` | str | `plinth` | `aud` claim |
| `PLINTH_IDENTITY_DEFAULT_TTL_SECONDS` | int | `3600` | Token lifetime default |
| `PLINTH_IDENTITY_MAX_TTL_SECONDS` | int | `86400` | Hard cap on issuance ttl |
| `PLINTH_IDENTITY_KEY_ROTATION_DAYS` | int | `30` | RS256 rotation cadence |
| `PLINTH_IDENTITY_KEYS_DIR` | path | `$DATA_DIR/identity-keys/` | RSA keypair storage |
| `PLINTH_IDENTITY_KEYS_ENCRYPTION_KEY` | base64 | auto-gen dev key | AES-GCM key for at-rest private keys |
| `PLINTH_STORAGE_DRIVER` | enum | `sqlite` | sqlite/postgres |
| `PLINTH_DATABASE_URL` / `PLINTH_IDENTITY_DATABASE_URL` | URL | — | Postgres DSN |
| `PLINTH_AUTO_MIGRATE` | bool | `true` | |

#### 3.3.4 Endpoints

[`CONTRACTS.md`](./CONTRACTS.md) §Identity Service.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Liveness |
| `POST` | `/v1/tokens` | Issue capability token |
| `POST` | `/v1/tokens/verify` | Verify (revocation-aware) |
| `POST` | `/v1/tokens/{jti}/revoke` | Revoke single token |
| `GET` | `/v1/tokens/{jti}` | Token info (no secret) |
| `GET` | `/v1/.well-known/jwks.json` | JWKS — last 3 non-expired keys |
| `GET` | `/v1/keys` | List signing keys (public PEM only) |
| `POST` | `/v1/keys/rotate` | Force rotation (admin) |
| `DELETE` | `/v1/keys/{kid}` | Expire key (admin) |
| `GET` | `/v1/revocations` | Cursor-paginated revocation feed (`?since=&limit=`) |
| `GET` | `/v1/revocations/stats` | Aggregate counts |
| `POST` | `/v1/tenants` | Create tenant |
| `GET` | `/v1/tenants` | List tenants |
| `GET` | `/v1/admin/migrations` | Migration status |
| `POST` | `/v1/admin/migrations/apply` | Apply pending |
| `POST` | `/v1/admin/migrations/rollback` | Rollback |

#### 3.3.5 Storage

| Table | Holds |
|---|---|
| `tokens` | Token metadata (`jti`, `agent_id`, `tenant_id`, `issued_at`, `expires_at`, `revoked`) |
| `signing_keys` | RSA keypairs (private encrypted with AES-GCM) |
| `tenants` | Tenant records |
| `revocations` | Revocation entries with `revoked_at` (cursor source for federation) |
| `schema_migrations` | Migration tracking |
| `$KEYS_DIR/<kid>.pem` | Encrypted private keys at rest |

#### 3.3.6 Dependencies

Standalone; only outbound dependency is the optional Postgres backend.

#### 3.3.7 Test Suite

166 tests. Coverage: HS256/RS256 token round-trip, JWKS endpoint correctness, key rotation overlap (last-3 verifying), revocation listing pagination, tenant CRUD, migration framework + rollback, advisory locks. Two known-flaky JWT-tampering tests (random byte mutations occasionally produce a still-valid signature) — see [`CHANGELOG.md`](./CHANGELOG.md) v0.5 §"Known limitations".

### 3.4 Dashboard Service

#### 3.4.1 Purpose

Read-only single-page web app + minimal proxy that aggregates Workspace + Gateway state for human operators. Polls upstream services every 5 s. Displays workspaces, channels, workflows (with v0.6 graph view), audit log, cache stats, cost rollups, OTLP status, and 60-minute time-series of tool calls per minute (vanilla `<canvas>`).

#### 3.4.2 Port and URL

`http://localhost:7424/` by default. Override via `PLINTH_DASHBOARD_PORT`.

#### 3.4.3 Configuration

| Variable | Type | Default | Meaning |
|---|---|---|---|
| `PLINTH_DASHBOARD_PORT` | int | `7424` | Bind port |
| `PLINTH_DASHBOARD_WORKSPACE_URL` | URL | `http://localhost:7421` | Upstream workspace |
| `PLINTH_DASHBOARD_GATEWAY_URL` | URL | `http://localhost:7422` | Upstream gateway |
| `PLINTH_DASHBOARD_MOCK_MCP_URL` | URL | `http://localhost:7423` | Optional, for mock-MCP info |
| `PLINTH_DASHBOARD_LOG_LEVEL` | enum | `INFO` | |

#### 3.4.4 Endpoints

[`services/dashboard/src/plinth_dashboard/server.py`](./services/dashboard/src/plinth_dashboard/server.py) header.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Static SPA shell |
| `GET` | `/static/{path}` | SPA assets |
| `GET` | `/healthz` | Liveness |
| `GET` | `/api/overview` | Aggregated cross-service summary |
| `GET` | `/api/workflows/overview` | Cross-workspace workflow list |
| `GET` | `/api/workspaces` | Proxy to workspace |
| `GET` | `/api/workspaces/{ws_id}` | Proxy |
| `GET` | `/api/workspaces/{ws_id}/kv` | Proxy |
| `GET` | `/api/workspaces/{ws_id}/snapshots` | Proxy |
| `GET` | `/api/workspaces/{ws_id}/channels` | Proxy |
| `GET` | `/api/workspaces/{ws_id}/workflows` | Proxy |
| `GET` | `/api/workspaces/{ws_id}/workflows/{wf_id}` | Proxy (single workflow detail) |
| `GET` | `/api/audit` | Proxy to gateway |
| `GET` | `/api/cache-stats` | Proxy |
| `GET` | `/api/audit-stats` | Proxy |
| `GET` | `/api/tools` | Proxy |

#### 3.4.5 Storage

The dashboard owns no persistent storage. It is purely a proxy + static-asset server.

#### 3.4.6 Dependencies

Workspace + Gateway via HTTP (mocked with `respx` in tests).

#### 3.4.7 Test Suite

60 tests. Coverage: aggregator endpoint correctness, proxy passthrough, workflow viz endpoint, DLQ inspector, time-series graph data shape.

### 3.5 Mock MCP Server

#### 3.5.1 Purpose

A minimal, offline-first MCP-compatible server providing 6 demo tools. Used by all examples and tests. Built-in fixtures let the demos run without internet.

#### 3.5.2 Port and URL

`http://localhost:7423` by default. Override via `PLINTH_MOCK_PORT`.

#### 3.5.3 Configuration

| Variable | Type | Default | Meaning |
|---|---|---|---|
| `PLINTH_MOCK_PORT` | int | `7423` | Bind port |
| `PLINTH_MOCK_FIXTURES_DIR` | path | `examples/fixtures/` | Where `mock://` URLs resolve |
| `PLINTH_MOCK_LOG_LEVEL` | enum | `INFO` | |

#### 3.5.4 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Liveness |
| `GET` | `/tools` | List the 6 tools |
| `POST` | `/invoke/{tool_name}` | Invoke a tool |

Tool inventory ([`CONTRACTS.md`](./CONTRACTS.md) §"Mock MCP Server API"):

| `tool_id` | Description |
|---|---|
| `web.fetch` | Fetch a URL and return its text. `mock://...` URLs return canned fixtures; real `https://...` URLs go through `httpx`. |
| `web.search` | Mock web search returning canned results. |
| `fs.read` | Read a file relative to the fixtures dir. |
| `fs.write` | Write a file relative to the fixtures dir. |
| `notes.add` | Append a note (in-memory, per-process). |
| `notes.list` | List notes. |

#### 3.5.5 Storage / 3.5.6 Dependencies / 3.5.7 Test Suite

In-memory + filesystem fixtures at `$PLINTH_MOCK_FIXTURES_DIR`; no durable state. No service dependencies — designed as a leaf. ~33 tests covering fixture loading, every tool path, error envelopes.

### 3.6 GitHub MCP Server

#### 3.6.1 Purpose

Real GitHub REST API integration. Reads OAuth bearer from forwarded `Authorization` header (the gateway attaches it). Returns Plinth-shaped error envelopes.

#### 3.6.2 Port and URL

`http://localhost:7426` by default. Override via `PLINTH_GITHUB_MCP_PORT`.

#### 3.6.3 Configuration

| Variable | Type | Default | Meaning |
|---|---|---|---|
| `PLINTH_GITHUB_MCP_PORT` | int | `7426` | Bind port |
| `PLINTH_GITHUB_MCP_LOG_LEVEL` | enum | `INFO` | |
| `PLINTH_GITHUB_API_BASE` | URL | `https://api.github.com` | Override for GitHub Enterprise |

#### 3.6.4 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Liveness |
| `GET` | `/tools` | Tool list |
| `POST` | `/invoke/{tool_id}` | Invoke a tool |

Tool inventory ([`CONTRACTS.md`](./CONTRACTS.md) §"GitHub MCP server"):

| `tool_id` | Purpose | Required scopes |
|---|---|---|
| `github.list_issues` | List issues in a repo | `repo` |
| `github.get_issue` | Get issue + comments | `repo` |
| `github.create_issue` | Create issue | `repo` |
| `github.update_issue` | Edit title/body/labels/state | `repo` |
| `github.comment_on_issue` | Add comment | `repo` |
| `github.get_repo` | Repo metadata | `repo` |
| `github.search_code` | Code search | `repo` |

#### 3.6.5 Storage / 3.6.6 Dependencies / 3.6.7 Test Suite

No persistent storage. Outbound dependency: GitHub REST API (the gateway owns OAuth lifecycle). 42 tests, all using `respx` to mock GitHub responses.

### 3.7 Slack MCP Server

#### 3.7.1 Purpose

Real Slack Web API integration. Same forward-the-bearer pattern as GitHub.

#### 3.7.2 Port and URL

`http://localhost:7427` by default. Override via `PLINTH_SLACK_MCP_PORT`.

#### 3.7.3 Configuration

| Variable | Type | Default | Meaning |
|---|---|---|---|
| `PLINTH_SLACK_MCP_PORT` | int | `7427` | |
| `PLINTH_SLACK_MCP_LOG_LEVEL` | enum | `INFO` | |
| `PLINTH_SLACK_API_BASE` | URL | `https://slack.com/api` | |

#### 3.7.4 Endpoints

| `tool_id` | Purpose |
|---|---|
| `slack.list_channels` | List public + private (where authorized) channels |
| `slack.post_message` | Post to a channel |
| `slack.list_messages` | Read recent messages |
| `slack.get_user` | User profile |

#### 3.7.5 Storage / 3.7.6 Dependencies / 3.7.7 Test Suite

No persistent storage. Outbound: Slack Web API. 23 tests using `respx`.

### 3.8 Linear MCP Server

#### 3.8.1 Purpose

Real Linear GraphQL API integration. Same pattern. Linear's GraphQL schema requires translating Plinth's REST-shaped tool calls into GraphQL queries; the translation lives in `mcp-servers/linear/src/linear_mcp/queries.py`.

#### 3.8.2 Port and URL

`http://localhost:7428` by default. Override via `PLINTH_LINEAR_MCP_PORT`.

#### 3.8.3 Configuration

| Variable | Type | Default | Meaning |
|---|---|---|---|
| `PLINTH_LINEAR_MCP_PORT` | int | `7428` | |
| `PLINTH_LINEAR_MCP_LOG_LEVEL` | enum | `INFO` | |
| `PLINTH_LINEAR_API_BASE` | URL | `https://api.linear.app/graphql` | |

#### 3.8.4 Endpoints

| `tool_id` | Purpose |
|---|---|
| `linear.list_issues` | List issues (filterable by team / assignee / state) |
| `linear.get_issue` | Issue details |
| `linear.create_issue` | Create issue |
| `linear.update_issue` | Update title / description / state / labels |
| `linear.comment_on_issue` | Add comment |

#### 3.8.5 Storage / 3.8.6 Dependencies / 3.8.7 Test Suite

No persistent storage. Outbound: Linear GraphQL API. 27 tests using `respx`.

---

## 4. SDK Reference

### 4.1 Python SDK (`plinth`)

#### 4.1.1 Installation, Import, Client Construction

```bash
pip install -e ./sdk/python
```

```python
from plinth import Plinth

# Permissive mode — any non-empty token works for v0.1 compat
client = Plinth(
    workspace_url="http://localhost:7421",
    gateway_url="http://localhost:7422",
    identity_url="http://localhost:7425",   # optional; only needed for capability flows
    api_key="local-dev",
)
```

When `verify_local`/`verify_remote` mode is enabled on the services, `api_key` must be a JWT issued by Identity (see [§7.4](#74-token-issuance-identity-service)).

#### 4.1.2 Surface Map

The Python SDK is structured as facades on the `Plinth` client. Each namespace mirrors a concern in the substrate:

```python
client.workspace("name")              # → Workspace handle (gets-or-creates)
ws.kv                                 # → KVProxy
ws.files                              # → FilesProxy
ws.snapshots                          # → SnapshotProxy (also ws.snapshot(...) shortcut)
ws.branch(...) / ws.with_branch(id)   # → branch ops
ws.channels                           # → ChannelsProxy
ws.workflows                          # → WorkflowsProxy
ws.locks                              # → LocksProxy

client.tools                          # → ToolGateway: invoke / dry_run / register / audit / cache
client.identity                       # → IdentityClient: issue/verify/revoke + tenants + keys + revocations
client.gateway.transaction(...)       # → TransactionBuilder (Saga)
client.workers                        # → WorkersClient: register / drain / list
client.workflow_runtime               # → WorkflowRuntime (decorator + worker glue)

client.count_tokens(text)             # tiktoken cl100k_base, offline
```

Source: [`sdk/python/src/plinth/__init__.py`](./sdk/python/src/plinth/__init__.py).

#### 4.1.3 Per-Namespace Method List

**`workspace.kv` (`KVProxy`)**

```python
kv.set(key, value)                    # PUT versioned write
kv.get(key, version=None, with_version=False)
kv.delete(key)                        # tombstone
kv.history(key)                       # all versions
kv.list()                             # latest of every key
```

**`workspace.files` (`FilesProxy`)**

```python
files.write(path, content_bytes_or_str, content_type=None)
files.read(path, version=None) -> bytes
files.meta(path) -> FileEntry
files.delete(path)
files.list()
```

**`workspace.snapshots` (`SnapshotProxy`)**

```python
snapshots.create(name, message=None) -> Snapshot
snapshots.list()
snapshots.get(snap_id)
snapshots.diff(left_snap_id, right_snap_id) -> DiffResult
ws.snapshot("baseline")               # shorthand
```

**`workspace.branches`**

```python
ws.branch(name, from_snapshot=snap.id) -> Branch
ws.branches() -> list[Branch]
ws.with_branch(branch_id)             # returns scoped Workspace whose ops target the branch
ws.merge_branch(branch_id) -> MergeResult
```

**`workspace.channels` (`ChannelsProxy`)**

```python
channels.send(name, payload, sender=None, type=None, correlation_id=None, headers=None)
channels.receive(name, since=0, limit=100, consumer=None, peek=False) -> list[ChannelMessage]
channels.ack(message)                 # delete by id
channels.list()
channels.set_schema(name, schema_dict)
channels.get_schema(name)
channels.deadletter(name) -> list[ChannelMessage]
channels.replay(message_id)
channels.replay_all_dlq(name, dry_run=False, max=100) -> ReplayBatchResult
channels.purge_dlq(name, older_than_seconds=86400)
channels.check_schema(name, schema, scope="both") -> SchemaCheckResult
```

**`workspace.workflows` (`WorkflowsProxy`)**

```python
workflows.create(name, steps=[...], metadata=None) -> WorkflowHandle
workflows.get(wf_id) -> WorkflowHandle
workflows.list() -> list[Workflow]

# WorkflowHandle:
wf.start_step(name, input=None) -> WorkflowStep
wf.complete_step(step_id, output=None, snapshot_id=None)
wf.fail_step(step_id, error=...)
wf.cancel()
wf.resume_info() -> ResumeInfo
wf.refresh()
```

**`workspace.locks` (`LocksProxy`)**

```python
with ws.locks.acquire("kv:sources/index", holder="agent-A", ttl_seconds=30):
    ws.kv.set("sources/index", new_value)

# Or low-level:
lock = ws.locks.acquire("name", holder="…", ttl_seconds=60)
ws.locks.heartbeat("name", holder="…")
ws.locks.release("name", holder="…")
ws.locks.list()
ws.locks.get("name")
```

**`tools` (`ToolGateway`)**

```python
tools.register(ToolRegistration(...))
tools.invoke(tool_id, args, workspace_id=None, agent_id=None, cache=True, idempotency_key=None) -> InvokeResponse
tools.dry_run(tool_id, args) -> DryRunResponse
tools.audit(workspace_id=None, tool_id=None, since=None, limit=100) -> list[AuditEvent]
tools.audit_stats(workspace_id=None) -> dict
tools.cache_stats() -> CacheStats
tools.cache_clear(tool_id=None)
tools.list()
```

**`identity` (`IdentityClient`)**

```python
identity.issue_token(agent_id, scopes, workspace_id=None, ttl_seconds=3600, metadata=None) -> TokenIssueResponse
identity.verify_token(token) -> TokenClaims
identity.revoke_token(jti)
identity.list_tokens(agent_id=None) -> list[TokenInfo]
identity.list_tenants() -> list[Tenant]
identity.create_tenant(name) -> Tenant
identity.list_keys() -> list[SigningKey]
identity.rotate_key() -> SigningKey
identity.expire_key(kid)
identity.list_revocations(since=0, limit=1000) -> RevocationList
```

**`gateway.transaction(...)` (`TransactionBuilder`)**

```python
tx = client.gateway.transaction(workspace_id=ws.id, agent_id="my-agent")
tx.add(
    "github.create_issue",
    {"repo": "owner/name", "title": "..."},
    compensation=("github.update_issue",
                  {"repo": "owner/name", "issue_number": "{result.number}", "state": "closed"}),
)
tx.add("slack.post_message", {"channel": "C123", "text": "{seq.0.result.html_url}"})
result = tx.commit()         # or tx.rollback()
print(result.status, result.calls)
```

**Worker runtime**

```python
@client.workflow_handler("research-pipeline", step="search")
def handle_search_step(ctx: HandlerContext, step):
    return {"sources": [...]}

client.run_workflow_worker(concurrency=4)   # blocking; runs the worker loop
```

#### 4.1.4 Error Class Hierarchy

```
PlinthError                           # base; every typed error inherits
├── NotFoundError
│   ├── WorkspaceNotFound
│   ├── KeyNotFound
│   ├── FileNotFound
│   ├── SnapshotNotFound
│   ├── BranchNotFound
│   ├── ChannelNotFound
│   ├── MessageNotFound
│   ├── WorkflowNotFound
│   ├── WorkflowStepNotFound
│   ├── WorkerNotFound
│   ├── ToolNotFound
│   ├── LockNotFound
│   └── TransactionNotFound
├── ValidationError
│   ├── InvalidArguments
│   ├── InvalidStepName / InvalidWorkflowStep
│   └── SchemaViolation                 # channel JSON-Schema violation
├── Unauthorized
│   ├── InvalidToken
│   ├── TokenExpired
│   └── TokenRevoked
├── RateLimited
├── CostCapExceeded
├── ToolInvocationError
├── LeaseConflict / LeaseNotHeld
├── LockConflict / LockNotHeld
├── TransactionFailed / TransactionInvalidStatus
└── NoHandlerError
```

Defined in [`sdk/python/src/plinth/exceptions.py`](./sdk/python/src/plinth/exceptions.py); re-exported from `plinth/__init__.py`.

#### 4.1.5 Token Counting + Cost Estimate

```python
client.count_tokens("text...")               # cl100k_base via tiktoken, exact
# Cost estimates: examples use Anthropic Sonnet pricing ($3/M in, $15/M out).
```

The token counter is offline (no network call) and uses the same encoding as Anthropic Sonnet, OpenAI GPT-4o, etc.

### 4.2 TypeScript SDK (`@plinth/sdk`)

#### 4.2.1 Surface

```typescript
import { Plinth } from "@plinth/sdk";

const client = new Plinth({
  workspaceUrl: "http://localhost:7421",
  gatewayUrl:   "http://localhost:7422",
  identityUrl:  "http://localhost:7425",
  apiKey:       "local-dev",
});

const ws = await client.workspace("research-task-1");
await ws.kv.set("topic", "renewable energy");
const result = await client.tools.invoke("web.fetch", { url: "..." });
const snap = await ws.snapshot("baseline");
```

Layout: [`sdk/typescript/src/`](./sdk/typescript/src/) — `client.ts`, `workspace.ts`, `tools.ts`, `channels.ts`, `workflows.ts`, `identity.ts`, `tokens.ts`, `errors.ts`, `types.ts`. ESM-only, Node 20+. Browser support is **not** an explicit goal for v0.6 (uses `fetch` so it should work, but isn't tested there).

#### 4.2.2 Parity Matrix vs Python

| Capability | Python | TypeScript | Notes |
|---|---|---|---|
| Workspace KV / files | ✅ | ✅ | Full parity |
| Snapshots / branches | ✅ | ✅ | |
| Channels (incl. typed + DLQ) | ✅ | ✅ | DLQ + schema-migration helpers added v0.6 |
| Workflows | ✅ | ✅ | |
| Generic locks | ✅ | ✅ (`ws.locks.withLock(...)`) | Both expose context-manager-style helper |
| Tools | ✅ | ✅ | |
| Identity (issue/verify/revoke/keys/revocations) | ✅ | ✅ | |
| Saga transactions | ✅ | ✅ | |
| Worker harness (durable executor) | ✅ | ❌ | Deferred to v0.7 (see [`ROADMAP.md`](./ROADMAP.md) §v0.7) |
| Token counting | ✅ tiktoken cl100k | ✅ `gpt-tokenizer` (~150 KB BPE) | Equivalent BPE encoding |
| OTLP self-emit | ❌ | ❌ | Gateway emits centrally |

The Python SDK is the canonical surface; the TypeScript SDK ships with 118 tests and holds parity at the API level for everything except worker handlers. See [`CHANGELOG.md`](./CHANGELOG.md) v0.6 §"Known limitations".

---

## 5. The Worker Runtime

### 5.1 What Workers Do

A worker is a long-running Python process that pulls **pending workflow steps** from one or more workspaces, leases them (so no other worker grabs the same step), executes the registered handler function, releases the lease with a result, and snapshots the workspace at step boundaries. Workers are how workflows graduate from "in-process and only-resumes-if-the-process-survives" (the v0.2 model) to "durable across restart, host loss, or process crash" (v0.5+).

### 5.2 The Handler Decorator Pattern

Handlers are registered against the Plinth client with `@client.workflow_handler(workflow_name, step=step_name)`. Source: [`sdk/python/src/plinth/workflow_runtime.py`](./sdk/python/src/plinth/workflow_runtime.py) and the spec in [`CONTRACTS.md`](./CONTRACTS.md) §"Durable Workflow Executor".

```python
# myapp/handlers.py
from plinth import Plinth, HandlerContext

client = Plinth(api_key="…", workspace_url="…", gateway_url="…")

@client.workflow_handler("research-pipeline", step="search")
def handle_search(ctx: HandlerContext, step):
    topic = step.input["topic"]
    results = ctx.tools.invoke("web.search", {"query": topic, "k": 5})
    return {"sources": [r["url"] for r in results["results"]]}

@client.workflow_handler("research-pipeline", step="extract")
def handle_extract(ctx: HandlerContext, step):
    sources = step.input["sources"]
    extracted = []
    for url in sources:
        page = ctx.tools.invoke("web.fetch", {"url": url})
        extracted.append({"url": url, "content": page["content"]})
        ctx.workspace.kv.set(f"sources/{url}", page["content"])
    return {"extracted": extracted}
```

The decorator stashes the handler in `client.workflow_runtime`. When the worker boots, it imports the module specified by `--handlers-module` and rebuilds the dispatch table from `client.workflow_runtime.handlers`.

### 5.3 Worker Process Lifecycle

```
┌─ register ──────────────────────┐
│ POST /v1/workers/register       │  → worker_id (worker_<ulid>)
└────────────┬────────────────────┘
             │
             ▼
┌─ poll loop ──────────────────────────────────────────────────┐
│ every 1-5s (configurable):                                   │
│   1. heartbeat: POST /v1/workers/{id}/heartbeat              │
│   2. for each managed workflow:                              │
│      pending = GET /v1/workspaces/{ws}/workflows/{wf}/pending│
│      for step in pending:                                    │
│         lease = POST /steps/{step.id}/lease (ttl=lease-ttl)  │
│         if 409 → continue (someone else got it)              │
│         try:                                                 │
│           handler = dispatch[(workflow, step.name)]          │
│           output = handler(ctx, step)                        │
│           snapshot = ctx.workspace.snapshot(...)             │
│           POST /steps/{step.id}/release status=completed     │
│             output=output snapshot_id=snap.id                │
│         except Exception as e:                               │
│           POST /steps/{step.id}/release status=failed        │
│             error=str(e)                                     │
│   3. while a handler runs:                                   │
│      heartbeat: POST /steps/{step.id}/heartbeat              │
│        every <heartbeat-interval>                            │
└────────────┬─────────────────────────────────────────────────┘
             │ SIGTERM / SIGINT
             ▼
┌─ drain ─────────────────────────┐
│ POST /v1/workers/{id}/drain     │  no new leases, finish in-flight
└────────────┬────────────────────┘
             │ on completion
             ▼
            exit 0
```

### 5.4 Crash Recovery Semantics

The lease reaper inside the workspace service (`LeaseStore.lease_reaper_loop` in `services/workspace/src/plinth_workspace/leases.py`) sweeps `workflow_step_leases WHERE expires_at < NOW() AND status='running'` every `PLINTH_LEASE_REAPER_INTERVAL_SECONDS` (default 30). Stale leases are marked `expired`, freeing the step for another worker.

**Expiry timeline** (with default values):

```
t=0     worker A leases step → expires_at = NOW()+60
t=15    worker A heartbeats → expires_at = NOW()+60 (now t=75)
t=30    worker A heartbeats → expires_at = NOW()+60 (now t=90)
t=45    worker A CRASHES — no more heartbeats
t=90    lease expires (no heartbeat for 60 s)
t=120   reaper sweeps (next 30 s tick) → status=expired
t=120+  any worker B can now POST /lease and acquire the step
```

If worker B finishes a step that worker A also "completed" because A was a zombie that came back: each `release` is idempotent on its own `worker_id` — the workspace rejects releases from non-holders with 409. Steps can therefore have `attempt > 1` if a previous attempt expired; the workflow log records every attempt distinctly.

### 5.5 Running Workers — `plinth-workflow-worker`

```bash
plinth-workflow-worker \
  --workspace-url    http://localhost:7421 \
  --gateway-url      http://localhost:7422 \
  --identity-url     http://localhost:7425 \
  --api-key          "$(cat .plinth-token)" \
  --concurrency      4 \
  --lease-ttl        60 \
  --heartbeat-interval 15 \
  --poll-interval    2 \
  --handlers-module  myapp.handlers \
  --log-level        INFO
```

| Flag | Default | Meaning |
|---|---|---|
| `--workspace-url` | `http://localhost:7421` | Workspace base URL |
| `--gateway-url` | `http://localhost:7422` | Gateway base URL |
| `--identity-url` | `http://localhost:7425` | Identity base URL (for token issue/refresh) |
| `--api-key` | env `PLINTH_WORKER_TOKEN` | Bearer for outbound calls |
| `--concurrency` | `4` | Async tasks running handlers in parallel |
| `--lease-ttl` | `60` | Seconds before a held lease expires |
| `--heartbeat-interval` | `15` | Per-step heartbeat cadence (must be < ttl) |
| `--poll-interval` | `2` | Pending-step poll cadence |
| `--handlers-module` | required | Importable Python module that registers handlers |
| `--log-level` | `INFO` | DEBUG/INFO/WARNING/ERROR |
| `--worker-id` | auto-gen | Override worker id (sticky — useful for tests) |

Source: `worker/src/plinth_workflow_worker/__main__.py` and `worker.py`.

### 5.6 Concurrency Tuning

| Knob | Where | Trade-off |
|---|---|---|
| `--concurrency` | worker | More concurrent steps → more leases held → faster throughput, more pressure on workspace + gateway. Start at 4 per CPU core. |
| `--lease-ttl` vs `--heartbeat-interval` | worker | Heartbeat interval should be 1/4 to 1/3 of TTL. Too long → slower failover; too short → heartbeat traffic dominates. |
| `PLINTH_LEASE_REAPER_INTERVAL_SECONDS` | workspace | Lower → faster reclaim of crashed workers' steps. Default 30 s is a good balance. |
| `--poll-interval` | worker | Polling interval for pending-step scan. Lower → faster pickup, more idle requests. |

---

## 6. The Five Demos

Every demo is a standalone Python project under `examples/` with its own `pyproject.toml` and entry-point. They are installed by `make install-examples` and runnable from `make demo*` targets.

### 6.1 Demo 01 — Research Agent (Token Comparison)

- **What it shows**: Plinth's central thesis. Same agent task (search 5 sources, write a report) executed two ways: (a) baseline, all state in chat history; (b) Plinth, sources stored in workspace KV referenced by key, gateway-cached fetches.
- **Headline result**: 71.3 % token reduction across 3 bundled topics (`renewable energy`, `ai agents`, `climate policy`).
- **Files involved**: [`examples/01-research-agent/`](./examples/01-research-agent/) — `compare.py`, `agent_baseline.py`, `agent_plinth.py`, fixtures under `examples/fixtures/`.
- **Run**: `make demo` (or `cd examples/01-research-agent && python compare.py --topic "renewable energy"`)
- **Expected output**:
  ```
    Baseline (no Plinth):        23,704 tokens   |   $0.0810
    With Plinth:                  6,795 tokens   |   $0.0345
    Reduction:                     71.3 %        |   $0.0464 saved
    Wall-clock:        Baseline 0.1 s   |   Plinth 0.2 s
    Tool calls:        Baseline   6     |   Plinth   6  (cached on second run)
  ```
- **How to extend**: drop new topics into `examples/fixtures/`; pass `--topic`. The token counter uses tiktoken `cl100k_base`; pricing is Anthropic Sonnet ($3/M in, $15/M out), override-able in `compare.py:price_estimate`.

### 6.2 Demo 02 — Multi-Agent Handoff (Channels)

- **What it shows**: three agents (Researcher → Writer → Reviewer) collaborating via durable workspace channels with bounded prompt sizes. ~8.7 k tokens total across the three agents.
- **Files involved**: [`examples/02-multi-agent-handoff/`](./examples/02-multi-agent-handoff/) — `orchestrate.py`, `agents/{researcher,writer,reviewer}.py`.
- **Run**: `make demo-handoff`
- **Expected output**:
  ```
  [orchestrator] starting Researcher → Writer → Reviewer pipeline
  [researcher]   sent  research-out seq=1 (5 sources)
  [writer]       received research-out, drafted report (1247 tokens)
  [writer]       sent  draft-out seq=1
  [reviewer]     received draft-out, approved
  [orchestrator] complete — total 8,712 tokens
  ```
- **How to extend**: add a 4th agent under `agents/` and wire it into `orchestrate.py`. Use `ws.channels.set_schema(...)` to enforce inter-agent message contracts.

### 6.3 Demo 03 — Resumable Workflow (Crash + Resume)

- **What it shows**: a 6-step deep-research pipeline that crashes mid-flight, then resumes from the most recent snapshot. ~32 % token saving versus restart-from-scratch.
- **Files involved**: [`examples/03-resumable-workflow/`](./examples/03-resumable-workflow/) — `crash_resume.py`, `pipeline.py`.
- **Run**: `make demo-resume`
- **Expected output**:
  ```
  [run 1] step search/fetch/extract  → completed
  [run 1] step synthesize            → CRASH (simulated)

  [run 2] resume_info: next_step=critique  snapshot=snap_01HZS...
  [run 2] step critique/finalize     → completed

  Total tokens (with resume):    7,415
  Tokens if restarted:          10,891   (32 % saved by resuming)
  ```
- **How to extend**: change the manifest in `pipeline.py:STEPS`. Raise an exception at any step to simulate a different crash point.

### 6.4 Demo 04 — GitHub Issue Triage (OAuth)

- **What it shows**: an agent classifies GitHub issues into bug/feature/question/spam buckets and writes a markdown triage report. Real OAuth flow via the gateway; simulation mode bundles 10 issue fixtures.
- **Files involved**: [`examples/04-github-issue-triage/`](./examples/04-github-issue-triage/) — `triage_agent.py`, `report_writer.py`.
- **Run**:
  ```bash
  make demo-triage
  # live mode (after OAuth consent):
  python examples/04-github-issue-triage/triage_agent.py --repo owner/name --limit 10 --mode live
  ```
- **Expected output**:
  ```
  [triage] fetched 10 issues from demo/repo (simulated)
  [triage] classified — bug: 4, feature: 3, question: 2, spam: 1
  [triage] wrote report to ws_<id>:files/triage-report.md (1873 bytes)
  ```
- **How to extend**: tweak `triage_agent.py:CATEGORIES`. For live mode: register a GitHub OAuth app, set `PLINTH_OAUTH_GITHUB_*`, restart the gateway, complete consent. See [§8.5](#85-per-tool-linking-auth_methodoauth2).

### 6.5 Demo 05 — Durable Workflow (Workers + Leases)

- **What it shows**: agents register handlers via `@client.workflow_handler`, workers poll/lease/execute. Killing a worker mid-step → another worker picks up after the lease expires.
- **Files involved**: [`examples/05-durable-workflow/`](./examples/05-durable-workflow/) — `start_workflow.py`, `handlers.py`.
- **Run** (two terminals):
  ```bash
  # Terminal 1
  plinth-workflow-worker --handlers-module handlers --concurrency 2

  # Terminal 2
  python examples/05-durable-workflow/start_workflow.py --topic "renewable energy"

  # Demonstrate recovery: SIGKILL terminal 1, restart it with the same flags;
  # after ~90s the expired lease is reaped and the new worker picks up.
  ```
- **Expected output**:
  ```
  [start]    created workflow wf_01HZQR... (steps: search, fetch, extract, synthesize)
  [worker-A] leased step search   (ttl=60s)
  [worker-A] released search      → completed
  [worker-A] leased step fetch    (ttl=60s)
  *** worker-A KILLED ***
  [worker-B] leased step fetch    (attempt=2, ttl=60s)
  [worker-B] released fetch       → completed
  [start]    workflow complete in 47s
  ```
- **How to extend**: add steps to `handlers.py`. A `raise` inside a handler marks the step `failed`; to retry, call `wf.start_step(name)` again — a new row with `attempt = N+1` is created.

---

## 7. Authentication & Multi-Tenancy

### 7.1 Auth Modes

Both Workspace and Gateway honour a three-mode `PLINTH_AUTH_MODE`:

| Mode | What it does | When to use |
|---|---|---|
| `permissive` (default) | Accepts any non-empty `Authorization: Bearer <token>` header. Records `tenant_id="default"`. | v0.1-style local dev; demos |
| `verify_local` | Verifies JWT locally (HS256 with shared secret, **or** RS256 via JWKS fetched from Identity). Cache 5 min. | Production with Identity service available |
| `verify_remote` | Calls `POST /v1/tokens/verify` on Identity for every request. Slower but always-fresh. | When local cache is unacceptable (tight revocation SLO) |

For RS256 + JWKS, the verifier fetches `${PLINTH_IDENTITY_URL}/v1/.well-known/jwks.json` on first use, caches keys for 5 minutes, and refreshes on a token-with-unknown-`kid`. See [§3.2.3](#323-configuration-1) and [`docs/architecture/06-identity-capabilities.md`](./docs/architecture/06-identity-capabilities.md).

### 7.2 JWT Capability Tokens — Claim Format

```json
{
  "sub":   "agt_01HZQR...",
  "iss":   "http://identity:7425",
  "aud":   "plinth",
  "iat":   1746446400,
  "exp":   1746450000,
  "jti":   "tok_01HZQR...",
  "agent_id":   "agt_01HZQR...",
  "tenant_id":  "default",
  "workspace_id": "ws_01HZQR..." ,
  "scopes": [
    "tool:web.fetch:read",
    "workspace:ws_01HZQR...:write"
  ],
  "rate_limit": {
    "rpm": 120,
    "burst": 40,
    "cost_cap_usd_hour": 5.0,
    "cost_cap_usd_day":  50.0
  }
}
```

Algorithms: **HS256** with shared secret (`PLINTH_IDENTITY_JWT_SECRET`, base64-encoded ≥32 random bytes) or **RS256** with auto-rotated 2048-bit RSA keys (`PLINTH_IDENTITY_JWT_ALG=RS256`). Schema: [`specs/schemas/capability-token.schema.json`](./specs/schemas/capability-token.schema.json).

### 7.3 Scope Grammar

| Pattern | Grants |
|---|---|
| `tool:<tool_id>` | Invoke any operation on this tool |
| `tool:<tool_id>:read` | Read-side-effect operations only |
| `tool:<tool_id>:write` | Write-side-effect operations only |
| `tool:<tool_id>:execute` | Execute-side-effect operations only |
| `workspace:<ws_id>:read` | Read KV / files / snapshots |
| `workspace:<ws_id>:write` | Write KV / files; create snapshots; merge branches |
| `workspace:<ws_id>:admin` | Plus delete / GC / retention |
| `tenant:<tenant_id>:admin` | Manage tokens within tenant |
| `*` | Superuser (issuance-time only, never via UI) |

The verifier matches longest-prefix-wins. `tool:web.fetch` covers `tool:web.fetch:read` and `tool:web.fetch:write`. See [`CONTRACTS.md`](./CONTRACTS.md) §"Scope grammar".

### 7.4 Token Issuance (Identity Service)

```python
from plinth import Plinth

client = Plinth(identity_url="http://localhost:7425", api_key="<bootstrap>")

response = client.identity.issue_token(
    agent_id="my-agent",
    scopes=["tool:web.fetch:read", "workspace:ws_01HZQR:write"],
    workspace_id="ws_01HZQR",
    ttl_seconds=3600,
    metadata={"reason": "research-task-2026-Q2"},
)

print(response.token)              # the JWT, hand to your agent
print(response.jti)                # token id, useful for revocation later
print(response.expires_at)
```

Server-side: [`POST /v1/tokens`](./services/identity/src/plinth_identity/api.py) with `TokenIssueRequest`, validates scopes against tenant policy (admin issuance), signs with the active key, persists `(jti, agent_id, tenant_id, issued_at, expires_at)` for revocation tracking.

### 7.5 Token Verification — Local Cache vs Remote

```
verify_local (HS256):
  decode + verify signature with PLINTH_IDENTITY_JWT_SECRET
  check exp / nbf / aud / iss
  check revocation cache (if PLINTH_REVOCATION_POLL_URL set)
  return claims

verify_local (RS256):
  decode header → kid
  if kid not in JWKS cache → fetch JWKS from Identity, cache 5min
  verify signature with public key for kid
  check exp / nbf / aud / iss
  check revocation cache
  return claims

verify_remote:
  POST {identity_url}/v1/tokens/verify {token}
  → 200 TokenClaims | 401
```

`verify_local` is the production default. Latency overhead per request: ~200 µs (HS256) / ~400 µs (RS256, after first JWKS fetch).

### 7.6 Federated Revocation (the v0.6 Polling Cache)

Identity exposes [`GET /v1/revocations?since=<unix_ts>&limit=<int>`](./services/identity/src/plinth_identity/api.py) returning a cursor-paginated `RevocationList`. Workspace and Gateway each maintain an in-memory `set[str]` of revoked JTIs (`RevocationCache` class), refreshed every `PLINTH_REVOCATION_POLL_INTERVAL_SECONDS` (default 60).

```python
class RevocationEntry(BaseModel):
    jti: str
    revoked_at: datetime
    agent_id: str
    tenant_id: str

class RevocationList(BaseModel):
    revocations: list[RevocationEntry]
    next_since: int          # cursor for next poll
    has_more: bool
```

Verification path: after signature + expiry checks pass, the verifier checks `jti in revocation_cache`; if so, raises `TokenRevoked`. **Failure mode**: if Identity is unreachable, the cache stays as-is (logs a warning every poll); newly issued revocations don't propagate until polling resumes. This is documented in [`CONTRACTS.md`](./CONTRACTS.md) §v0.6 "Federated Revocation". Disable polling by leaving `PLINTH_REVOCATION_POLL_URL=""` (the default).

### 7.7 Multi-Tenancy

Every state-bearing table in Workspace, Gateway, and Identity has a `tenant_id TEXT NOT NULL DEFAULT 'default'` column ([`CONTRACTS.md`](./CONTRACTS.md) §"Multi-Tenancy Across Services"). Auth middleware extracts `tenant_id` from the verified JWT claim (or falls back to `"default"` in `permissive` mode). All list/query endpoints filter by `tenant_id`, so:

- A token in tenant `acme-corp` cannot see workspaces in tenant `globex-inc`.
- `GET /v1/workspaces` returns only the caller's tenant's workspaces.
- `GET /v1/audit?workspace_id=...` only returns events for workspaces in the caller's tenant.
- `GET /v1/tenants` returns the tenants the caller has access to.

The model is **shared schema with tenant_id**, per [ADR 0006](./docs/adr/0006-multitenancy-model.md). The next isolation tier (schema-per-tenant) is on the roadmap behind the v0.7 "Multi-tenant SaaS Posture" milestone.

---

## 8. OAuth Integration

### 8.1 The Authorization Code + PKCE Flow

The Gateway is a **PKCE-correct** OAuth 2.0 Authorization Code client for GitHub, Slack, and Linear. The flow:

```
Agent → GET  /v1/oauth/github/authorize?redirect_uri=...&scopes=repo
                ↓ Gateway generates code_verifier + code_challenge,
                  stores state in oauth_state table (TTL 10 min)
        302 Redirect → https://github.com/login/oauth/authorize?...&code_challenge=...

User consents on GitHub.

GitHub → GET /v1/oauth/github/callback?code=...&state=...
                ↓ Gateway looks up state, exchanges code+code_verifier for access token
                ↓ Encrypts access_token + refresh_token with AES-256-GCM
                ↓ Persists OAuthConnection (conn_<ulid>)
        302 Redirect → original redirect_uri?connection_id=conn_...

Agent (later) → POST /v1/invoke {tool_id: "github.create_issue", arguments: {...}}
                ↓ Gateway looks up OAuthConnection, decrypts access_token,
                  attaches Authorization: Bearer <access_token> to outbound call
                ↓ proxies to github-mcp:7426
```

State protection: the state value is a 32-byte random + HMAC, stored in `oauth_state(state, code_verifier, expires_at, used)`. Replay protection: state is marked `used=true` on first redemption.

### 8.2 Provider Configuration

| Provider | Vars |
|---|---|
| GitHub | `PLINTH_OAUTH_GITHUB_CLIENT_ID`, `PLINTH_OAUTH_GITHUB_CLIENT_SECRET`, `PLINTH_OAUTH_GITHUB_REDIRECT_URI`, `PLINTH_OAUTH_GITHUB_SCOPES` (default `repo,read:user`) |
| Slack | `PLINTH_OAUTH_SLACK_CLIENT_ID`, `PLINTH_OAUTH_SLACK_CLIENT_SECRET`, `PLINTH_OAUTH_SLACK_REDIRECT_URI`, `PLINTH_OAUTH_SLACK_SCOPES` (default `channels:read,chat:write,users:read`) |
| Linear | `PLINTH_OAUTH_LINEAR_CLIENT_ID`, `PLINTH_OAUTH_LINEAR_CLIENT_SECRET`, `PLINTH_OAUTH_LINEAR_REDIRECT_URI`, `PLINTH_OAUTH_LINEAR_SCOPES` (default `read,write`) |

All providers share `PLINTH_OAUTH_ENCRYPTION_KEY` (32-byte base64, AES-256-GCM). When unset, the Gateway auto-generates a development key on startup and logs a loud warning — **production must set this explicitly**.

### 8.3 At-Rest Token Encryption

Access and refresh tokens are encrypted with **AES-256-GCM**, with a per-record nonce. Schema:

```sql
CREATE TABLE oauth_connections (
  id TEXT PRIMARY KEY,                    -- conn_<ulid>
  tenant_id TEXT NOT NULL DEFAULT 'default',
  provider TEXT NOT NULL,                  -- 'github' | 'slack' | 'linear'
  user_id TEXT NOT NULL,
  user_login TEXT,
  scopes_json TEXT NOT NULL,
  access_token_encrypted TEXT NOT NULL,    -- nonce || ciphertext || tag, base64
  expires_at TIMESTAMP,
  refresh_token_encrypted TEXT,
  created_at TIMESTAMP NOT NULL,
  last_refreshed_at TIMESTAMP
);
```

API responses use the `OAuthConnectionPublic` shape — tokens are **never** returned over the wire. See [`CONTRACTS.md`](./CONTRACTS.md) §"OAuth 2.0 Authorization Code Flow".

### 8.4 Connection Management (CRUD)

```python
# Initiate the flow (returns a URL the user must visit)
url = client.gateway.oauth_authorize_url(
    "github",
    redirect_uri="http://localhost:3000/callback",
    scopes=["repo"],
)

# After the user redirects back with ?code=...&state=...:
conn = client.gateway.oauth_complete("github", code="...", state="...")

# List connections in your tenant
conns = client.gateway.list_oauth_connections()

# Refresh an access token (proactive, before expiry)
refreshed = client.gateway.oauth_refresh(connection_id=conn.id)

# Revoke locally + best-effort at provider
client.gateway.delete_oauth_connection(connection_id=conn.id)
```

### 8.5 Per-Tool Linking (`auth_method=oauth2`)

Tools register with an `auth_config` referencing a connection lookup template:

```python
client.tools.register(ToolRegistration(
    tool_id="github.create_issue",
    name="Create GitHub Issue",
    description="Create a new issue in a repository",
    transport="http",
    endpoint="http://localhost:7426/invoke/github.create_issue",
    input_schema={...},
    output_schema={...},
    idempotent=False,
    side_effects="write",
    auth_method="oauth2",
    auth_config={
        "provider": "github",
        "connection_id_from": "agent.identity.workspace_id",
    },
))
```

On invoke, the gateway looks up the matching `OAuthConnection` for the agent's tenant + provider, decrypts the access token, and attaches `Authorization: Bearer <access_token>` to the outbound HTTP call.

### 8.6 Adding a New Provider — Step-by-Step

1. Pick a slug (e.g. `notion`).
2. Add `PLINTH_OAUTH_NOTION_*` env vars to `services/gateway/src/plinth_gateway/settings.py`.
3. Add a provider entry to `OAuthProviders` in `services/gateway/src/plinth_gateway/oauth.py` — `authorize_url`, `token_url`, `userinfo_url`, default scopes, response shape (Slack-style flat vs OAuth-2-standard nested — both are supported).
4. If the provider returns scope strings differently (e.g. Slack's flat `scope: "a,b,c"`), implement a `_parse_scopes` hook.
5. Build an MCP server under `mcp-servers/notion/` that reads `Authorization` from the forwarded request, calls the provider's API, and returns Plinth-shaped JSON.
6. Add tests under `services/gateway/tests/test_oauth_notion.py`.
7. Document scopes in [`CONTRACTS.md`](./CONTRACTS.md) §"Tool registration with OAuth".
8. Add a row to the demo-04-equivalent example if you want a runnable showcase.

---

## 9. Workflows in Depth

### 9.1 Workflow Lifecycle

A workflow has the following derived statuses (derived from the step log, not stored independently):

```
pending      → no step row exists yet
running      → at least one step is running OR completed but not all
completed    → every step in the manifest has a completed row
failed       → a step has status=failed AND no recovery attempt is running
cancelled    → workflow cancellation flag is set
```

Per-step states ([`docs/architecture/08-workflows-design.md`](./docs/architecture/08-workflows-design.md) §2):

```
pending → running → completed
                  ↘ failed
                  ↘ cancelled

(retries: a new step row with attempt=N+1 can be created for the same name)
```

### 9.2 Steps and Snapshots

Every step's `complete` call should reference a workspace snapshot. The snapshot pins exactly what state the step produced — so when another worker resumes after a crash, it can restore from that snapshot before running the next step.

```python
wf = ws.workflows.create("research-pipeline", steps=["search", "fetch", "extract"])

# Step 1
step_search = wf.start_step("search", input={"topic": "renewable energy"})
sources = ctx.tools.invoke("web.search", {"query": "renewable energy", "k": 5})
ws.kv.set("sources", sources["results"])
snap = ws.snapshot("after-search")
wf.complete_step(step_search.id, output={"sources": sources["results"]}, snapshot_id=snap.id)
```

### 9.3 Resumability Semantics

```python
wf2 = ws.workflows.get(wf.id)
resume = wf2.resume_info()
# resume = ResumeInfo(workflow_id=..., next_step="fetch", last_completed=<search step>,
#                    snapshot_id="snap_after-search")

if resume.next_step:
    # Optional: restore workspace files/KV by branching from snapshot
    branch = ws.branch("recovery", from_snapshot=resume.snapshot_id)
    # … continue at resume.next_step …
```

The contract: `next_step` is the **next manifest entry whose latest attempt is not `completed`**. Steps that failed can be retried by calling `start_step(name)` again with `attempt = N + 1`.

### 9.4 Durable Execution via Workers (vs In-Process)

The v0.2 model is **in-process**: an agent script creates a workflow, calls `start_step`, does the work, calls `complete_step`. If the script crashes, you must restart it; resume from the last snapshot is your responsibility.

The v0.5 **durable** model adds a worker pool that reads pending steps, leases them, and runs handler functions. The agent is now stateless — even the orchestrator can crash. See [§5](#5-the-worker-runtime).

| Scenario | In-process (v0.2) | Durable (v0.5+) |
|---|---|---|
| Agent script crashes | Workflow stalls until you restart and call `resume_info()` | Lease expires → another worker picks up |
| Worker process crashes | n/a | Lease expires after `lease-ttl` → another worker picks up |
| Workspace service crashes | All resume info already persisted; restart and continue | Same |
| Single-machine demos | Just run the agent | Need a worker process + start trigger |

Choose in-process for short flows where the orchestrator is reliable. Choose durable for production agents that must survive host loss.

### 9.5 Saga-Style Transactions (Forward + Compensation)

Source: [`CONTRACTS.md`](./CONTRACTS.md) §"Workflow Transactions with Compensating Actions".

```python
tx = client.gateway.transaction(workspace_id=ws.id, agent_id="my-agent")
tx.add(
    "github.create_issue",
    {"repo": "owner/name", "title": "..."},
    compensation=("github.update_issue",
                  {"repo": "owner/name", "issue_number": "{result.number}", "state": "closed"}),
)
tx.add(
    "slack.post_message",
    {"channel": "C123", "text": "Issue created: {seq.0.result.html_url}"},
    compensation=None,
)
result = tx.commit()      # or tx.rollback() to undo without committing
```

Commit semantics: calls execute in `seq` order. If any call fails, the transaction enters `compensating` and runs registered compensations on already-`committed` calls in **reverse** order, rendering `arguments_template` against the forward call's result. If the compensation cascade itself fails on a step, the transaction status becomes `failed`; manual intervention is required.

The argument template grammar:

| Placeholder | Resolves to |
|---|---|
| `{result.field}` | The `field` of the most recent forward call's result |
| `{seq.N.result.field}` | The `field` of call at position `N`'s result |

### 9.6 Argument Templates

```python
# Sequential references — call 0's result is referenced by call 2:
tx.add("a", {...})                           # seq 0
tx.add("b", {...})                           # seq 1
tx.add("c", {"prev_url": "{seq.0.result.html_url}"})   # seq 2 sees call 0's result

# Compensation references current-call's result:
tx.add(
    "github.create_issue",
    {"repo": "owner/name", "title": "x"},
    compensation=("github.update_issue", {"issue_number": "{result.number}", "state": "closed"}),
)
# {result} inside compensation is the FORWARD result of the same call.
```

---

## 10. Channels & Coordination

### 10.1 Channel Send/Receive Semantics

Source: [`docs/architecture/07-channels-design.md`](./docs/architecture/07-channels-design.md). Channels are **workspace-scoped**, **lazily-created on first send**, and **per-channel monotonically sequenced**.

```python
ws.channels.send(
    "research-out",
    {"topic": "renewable energy", "sources": [...]},
    sender="researcher",
    type="research-complete",
    correlation_id=None,
    headers={"priority": "high"},
)

messages = ws.channels.receive(
    "research-out",
    since=0,           # all messages
    limit=100,
    consumer="writer", # tracks per-consumer cursor server-side
    peek=False,        # if True, don't advance cursor
)
for msg in messages:
    process(msg.payload)
    ws.channels.ack(msg)   # delete by id
```

### 10.2 Consumer Cursors

When `consumer=<name>` is passed, the workspace tracks a per-consumer cursor (last seq seen). Subsequent `receive` calls without `since` resume from cursor. With `peek=true` the cursor is not advanced, useful for at-least-once-style replay tests.

### 10.3 Typed Channels (JSON Schema Attachment)

```python
ws.channels.set_schema("research-out", {
    "type": "object",
    "required": ["topic", "sources"],
    "properties": {
        "topic":   {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
})

# Send a malformed payload:
try:
    ws.channels.send("research-out", {"topic": "x"})    # missing 'sources'
except SchemaViolation as e:
    print(e.deadletter_msg_id)
    # The message is in the DLQ; a 422 was returned with details.
```

### 10.4 Dead-Letter Queue Routing + Replay

When validation fails, the message is delivered to a hidden `<channel>.deadletter` sub-channel **and** the original send returns 422 with `{"code":"SCHEMA_VIOLATION", "details":{"errors":[...], "deadletter_msg_id":"..."}}`.

```python
dlq = ws.channels.deadletter("research-out")
for msg in dlq:
    print(msg.id, msg.payload)

# Update schema, then replay:
ws.channels.replay(dlq[0].id)
```

### 10.5 Schema Migration Helpers (v0.6)

```python
# Preview compatibility before committing a new schema:
result = ws.channels.check_schema("research-out", new_schema, scope="both")
# SchemaCheckResult(channel=..., scope="both", checked=42,
#                   valid=39, invalid=3, sample_failures=[...])

# Bulk replay DLQ — dry-run first:
batch = ws.channels.replay_all_dlq("research-out", dry_run=True)
print(batch.attempted, batch.succeeded, batch.failed)

# Then for real:
ws.channels.replay_all_dlq("research-out", max=100)

# Hygiene — drop messages older than 24h:
ws.channels.purge_dlq("research-out", older_than_seconds=86400)
```

The Dashboard exposes "Replay all" and "Purge older than 24h" buttons in the per-channel DLQ panel ([`CHANGELOG.md`](./CHANGELOG.md) v0.6).

---

## 11. Observability

### 11.1 Audit Log (Per-Call Event Format)

Every `/v1/invoke` call writes one row to `audit_events`:

```python
class AuditEvent(BaseModel):
    id: str               # evt_<ulid>
    timestamp: datetime
    tool_id: str
    workspace_id: str | None
    agent_id: str | None
    arguments_hash: str   # sha256(canonical_json(args))
    result_hash: str      # sha256(canonical_json(result))
    cached: bool
    duration_ms: int
    cost_estimate_usd: float
    error: str | None
```

Append-only. No updates, no deletes (cryptographic chaining is post-v0.1). Queryable via `GET /v1/audit?workspace_id=&tool_id=&since=&limit=`.

### 11.2 OpenTelemetry Export

When `PLINTH_OTLP_ENABLED=true`, the gateway also emits each audit event as an **OTLP Log** (not a span) to `PLINTH_OTLP_ENDPOINT`. The exporter is buffered (`PLINTH_OTLP_BATCH_SIZE`) and flushed every `PLINTH_OTLP_FLUSH_INTERVAL_SECONDS`.

Log attributes (mirrors [`specs/schemas/event.schema.json`](./specs/schemas/event.schema.json)):

| Attribute | Source |
|---|---|
| `service.name` | `PLINTH_OTLP_SERVICE_NAME` (default `plinth-gateway`) |
| `tool.id` | `audit_event.tool_id` |
| `workspace.id` | `audit_event.workspace_id` |
| `agent.id` | `audit_event.agent_id` |
| `tenant.id` | from JWT claim |
| `cached` | bool |
| `duration_ms` | int |
| `cost_estimate_usd` | float |
| `result_hash`, `arguments_hash` | sha256 hex |
| `error` | string \| null |

Compatible with Datadog, Tempo, Honeycomb, and any OTLP/HTTP collector. Failure isolation: an emit failure never crashes the gateway; the buffer is dropped if the collector is unreachable for `2 × FLUSH_INTERVAL`. Inspect status via [`GET /v1/observability/status`](./services/gateway/src/plinth_gateway/otlp_api.py).

### 11.3 Cost Attribution

The audit log carries `cost_estimate_usd`. Aggregation happens server-side via:

- `GET /v1/audit/stats?workspace_id=...` — totals by workspace
- `GET /v1/limits/{agent_id}/status` — current rolling-window usage vs cap
- Dashboard `/api/overview` — cluster-wide rollups for the dashboard tenant

Cost estimates are **per-tool**, supplied at registration time via a `pricing_model` field (or hardcoded defaults). For LLM-equivalent estimates the SDK provides `client.count_tokens(...)` (cl100k_base / `gpt-tokenizer`) and the demo at `examples/01-research-agent/compare.py` shows usage. Pricing tables for built-in tools live in `services/gateway/src/plinth_gateway/pricing.py`.

### 11.4 Dashboard Panels

Routes ([`services/dashboard/src/plinth_dashboard/server.py`](./services/dashboard/src/plinth_dashboard/server.py) header):

- `/` — overview: workspaces, channels, workflows, recent audit, cache stats, cost rollups, OTLP status
- `/workflows` — list of all workflows across all workspaces (default tenant)
- `/workflows/{wf_id}?ws=<ws_id>` — graph view of a single workflow (SVG, status-colored nodes, click-for-detail modal)
- Per-workspace pages — KV browser, snapshot list, channel list, DLQ inspector with replay/purge buttons

The 60-minute time-series of tool calls per minute is computed from `audit_events` and rendered in vanilla `<canvas>` (no chart library). Auto-refresh every 5 s.

---

## 12. Production Hardening

### 12.1 Rate Limits & Cost Caps

Defaults (Gateway env vars, [§3.2.3](#323-configuration-1)):

```bash
PLINTH_RATE_LIMIT_DEFAULT_RPM=60
PLINTH_RATE_LIMIT_DEFAULT_BURST=20
PLINTH_COST_CAP_DEFAULT_USD_HOUR=1.0
PLINTH_COST_CAP_DEFAULT_USD_DAY=10.0
```

Per-agent override:

```bash
curl -X POST -H 'Content-Type: application/json' \
  -d '{"rpm": 120, "burst": 40, "cost_cap_usd_hour": 5.0, "cost_cap_usd_day": 50.0}' \
  http://localhost:7422/v1/limits/agt_01HZQR
```

Token bucket math: `capacity = burst`; `rate = rpm/60` tokens/sec. A fresh agent can issue `burst` calls instantly, then sustain `rpm/60` calls/sec. Refill is **lazy** on each call (no background timer). See [`docs/architecture/09-rate-limiting-design.md`](./docs/architecture/09-rate-limiting-design.md) §3.

429 response shape:

```json
{
  "error": {
    "code": "RATE_LIMITED",
    "message": "Rate limit exceeded for agent_id agt_X. Retry after 12s.",
    "details": {
      "limit_type": "rpm",
      "retry_after_seconds": 12,
      "current": 60,
      "limit": 60
    }
  }
}
```

`Retry-After` HTTP header is always set on 429.

### 12.2 Load Shedding

When `PLINTH_LOAD_SHED_ENABLED=true`, both Workspace and Gateway wrap requests in the `LoadShedder` middleware (`services/<svc>/src/plinth_<svc>/load_shed.py`). When `inflight + queued > max_inflight + max_queue`, the middleware returns:

```http
HTTP/1.1 503 Service Unavailable
Retry-After: 1
{"error":{"code":"LOAD_SHED","message":"Service overloaded — retry shortly"}}
```

Health endpoints (`/healthz`) are **exempt** so liveness probes never see 503s due to traffic load. Stats are exposed at `GET /v1/admin/load-shed/stats`. The benchmark suite is built to exercise this directly — see [§14](#14-performance-reference).

### 12.3 Postgres Backend Setup

```bash
# 1. Create the database and user
createdb plinth
psql plinth <<SQL
CREATE USER plinth_app WITH PASSWORD 'change-me';
GRANT ALL PRIVILEGES ON DATABASE plinth TO plinth_app;
SQL

# 2. Apply migrations per service
PLINTH_STORAGE_DRIVER=postgres \
PLINTH_DATABASE_URL=postgresql://plinth_app:change-me@localhost:5432/plinth \
  .venv/bin/python -m plinth_workspace migrate

PLINTH_STORAGE_DRIVER=postgres \
PLINTH_DATABASE_URL=postgresql://plinth_app:change-me@localhost:5432/plinth \
  .venv/bin/python -m plinth_gateway migrate

PLINTH_STORAGE_DRIVER=postgres \
PLINTH_DATABASE_URL=postgresql://plinth_app:change-me@localhost:5432/plinth \
  .venv/bin/python -m plinth_identity migrate

# 3. Start services with the env vars
make serve
```

Pool settings: `PLINTH_DB_POOL_MIN_SIZE=5`, `PLINTH_DB_POOL_MAX_SIZE=20`. The driver is `asyncpg` (no SQLAlchemy). Schema parity ([`CONTRACTS.md`](./CONTRACTS.md) §"Postgres Backend"):

| SQLite type | Postgres type |
|---|---|
| `TEXT` | `TEXT` |
| `INTEGER` | `BIGINT` (where overflow is plausible) |
| `TIMESTAMP` | `TIMESTAMPTZ` |
| JSON-as-`TEXT` | stays `TEXT` (no migration to `JSONB` in v0.6) |

### 12.4 Migrations (Forward + Rollback)

```bash
# Status
.venv/bin/python -m plinth_workspace migrate --status

# Apply all pending
.venv/bin/python -m plinth_workspace migrate

# Apply up to a specific id
.venv/bin/python -m plinth_workspace migrate --to 0003_workflows

# Roll back to a specific id (atomic per-step, requires <id>_rollback.sql for each step past target)
.venv/bin/python -m plinth_workspace migrate --rollback-to 0003_workflows
.venv/bin/python -m plinth_workspace migrate --rollback-to 0003_workflows --dry-run

# Scaffold a new migration file
.venv/bin/python -m plinth_workspace migrate --create "add foo column"
```

Equivalent admin endpoints:

```
GET  /v1/admin/migrations
POST /v1/admin/migrations/apply
POST /v1/admin/migrations/rollback   body: {"to": "0003_workflows", "dry_run": false}
```

Tracking table:

```sql
CREATE TABLE schema_migrations (
  id TEXT PRIMARY KEY,
  applied_at TIMESTAMP NOT NULL,
  duration_ms INTEGER NOT NULL,
  checksum TEXT NOT NULL,            -- sha256 of forward sql
  rollback_checksum TEXT             -- sha256 of <id>_rollback.sql, if present
);
```

The runner uses `pg_advisory_lock(<service-hash>)` on Postgres and `fcntl.flock` on SQLite, so multiple replicas can boot concurrently (v0.6).

### 12.5 Schema Migration Discipline

When to add a `<id>_rollback.sql`:

- **Always** for irreversible operations (DROP TABLE, DROP COLUMN, type changes).
- **Always** for changes that require backfill — the rollback should drop the backfilled column, not just the schema change.
- **Optional but recommended** for additive changes (new tables, new columns with defaults). The runner won't complain if missing, but you'll be unable to roll back past it.

If a rollback file is missing, `migrate --rollback-to ...` fails with `MIGRATION_ROLLBACK_MISSING` and stops — no partial rollback. See [`CONTRACTS.md`](./CONTRACTS.md) §"Migration Rollback".

### 12.6 Backups & Retention

Per-workspace retention policy:

```python
ws.set_retention(RetentionPolicy(
    workspace_id=ws.id,
    keep_versions=10,                 # keep last 10 versions per key/path
    keep_days=30,                     # OR keep versions newer than 30 days
    keep_snapshots=20,                # keep last 20 snapshots
    delete_unreferenced_blobs=True,
))

result = ws.gc()                       # run GC now
print(result.kv_versions_deleted, result.bytes_freed)

# Cluster-wide sweep (admin scope required):
client.gateway.admin_gc()
```

GC respects the **most permissive** of the active rules per row. Versions referenced by any non-deleted snapshot are preserved unconditionally — snapshots are roots for the version-retention algorithm. Blob files (`$DATA_DIR/blobs/<sha256>`) are deleted only if no `file_entries` row references them. GC is concurrent-safe via per-workspace advisory lock.

For backups: SQLite is `cp workspace.db.bak workspace.db` (use `sqlite3 .backup`); Postgres is `pg_dump`. Blob dir is content-addressed so `rsync -aP` is sufficient.

---

## 13. Operations Runbook

### 13.1 Service Won't Start — Diagnostic Ladder

1. **Check the log** — `tail -n 100 /tmp/plinth-logs/<svc>.log`. Most startup errors print a clear traceback.
2. **Port conflict?** — `lsof -i :7421` (or whichever port). Another process holding the port is the most common cause.
3. **Pid file stale?** — `cat /tmp/plinth-pids/<svc>.pid` and `ps -p <pid>`. If the pid isn't running, `rm /tmp/plinth-pids/<svc>.pid` and try again.
4. **Migrations failing?** — `.venv/bin/python -m plinth_<svc> migrate --status`. A `checksum mismatch` error means someone edited a previously-applied migration; revert the edit or roll back.
5. **Postgres unreachable?** — `psql $PLINTH_DATABASE_URL -c '\dt'`. Network/DNS or pg_hba.conf likely.
6. **Permissions on `$PLINTH_DATA_DIR`?** — service must be able to write to it. Check `ls -ld $PLINTH_DATA_DIR`.
7. **`PLINTH_AUTH_REQUIRED=true` but no `PLINTH_IDENTITY_JWT_SECRET`?** — startup fails with `MissingSecret`. Set it (≥32 base64 bytes) or unset `AUTH_REQUIRED`.

### 13.2 Slow Demo — Diagnostic Ladder

1. **Are services healthy?** — `make healthcheck`.
2. **Cache hits?** — `curl http://localhost:7422/v1/cache/stats`. If `hits==0` after the first run, caching may be disabled (`idempotent=false` on the tool, or `cache=false` passed by the caller).
3. **Rate limited?** — `GET /v1/limits/<agent_id>/status` — if `rpm_used_in_window` is at the cap, tune `PLINTH_RATE_LIMIT_DEFAULT_RPM` upward or set per-agent overrides.
4. **DB slow?** — SQLite WAL contention shows as `5xx database is locked`. On Postgres, check `pg_stat_activity` for long-running queries; missing indices appear in v0.5 if migrations weren't applied.
5. **Sync audit hot path** — every invocation writes to `audit_events`. If write IOPS are saturated, OTLP export does **not** replace the SQL audit (both write); plan to scale storage.
6. **OTLP backpressure** — `GET /v1/observability/status` shows `flush_errors`. A failing collector slows the gateway because the buffer fills synchronously after `BATCH_SIZE`.

### 13.3 Workflow Stuck in `running` — Diagnostic Ladder

1. **Get the workflow** — `GET /v1/workspaces/{ws}/workflows/{wf}`. Find the step with `status=running`.
2. **Check leases** — `GET /v1/workspaces/{ws}/workflows/{wf}/expired`. A step whose lease has expired but reaper hasn't swept yet looks "stuck".
3. **Worker alive?** — `GET /v1/workers?status=active`. Find the worker holding the lease; check its `last_heartbeat_at`.
4. **Reaper running?** — Workspace logs should show `lease_reaper.tick` every 30 s. If absent, set `PLINTH_LEASE_REAPER_ENABLED=true` and restart.
5. **Manual recovery** — `POST /steps/{step_id}/release {worker_id, status: "failed"}` from the same worker, or wait for lease expiry + reaper sweep, then have a fresh worker pick it up.

### 13.4 DLQ Filling Up — Operations Response

1. **Inspect** — `GET /v1/workspaces/{ws}/channels/{name}/deadletter` or use the dashboard DLQ panel.
2. **Check schema** — `GET /channels/{name}/schema`. If schema was changed recently and producers haven't caught up, this is expected.
3. **Validate sample** — `POST /channels/{name}/schema/check {schema, scope: "both", limit: 100}` — see how many of the in-flight messages would now pass.
4. **Replay all** — `POST /channels/{name}/deadletter/replay-all?dry_run=true` first; if results look right, drop `dry_run`.
5. **Purge old** — `DELETE /channels/{name}/deadletter?older_than_seconds=86400` (24 h is a sensible default).
6. **Long-term**: change the producer to validate before send, or relax the schema. Channel schemas with `additionalProperties: true` are forgiving by default.

### 13.5 Identity Revoke Not Propagating — Check Polling Settings

1. **Is polling enabled on the verifier?** — `PLINTH_REVOCATION_POLL_URL` set on Workspace and Gateway? Default is empty (= no polling).
2. **Polling interval** — `PLINTH_REVOCATION_POLL_INTERVAL_SECONDS` defaults to 60 s. So a revocation can take up to 60 s to propagate.
3. **Identity reachable?** — verifier logs `revocation_cache.refresh_failed` on every poll if not. Fix DNS/network.
4. **Cache sane?** — instruments expose `revocation_cache.size` via the gateway's `GET /v1/cache/stats` (if extended) or via OTLP.
5. **Forced freshness**: switch the verifier to `PLINTH_AUTH_MODE=verify_remote` for tight SLOs — every request hits Identity directly.

### 13.6 OAuth Token Refresh Failing — Debugging

1. **Try a manual refresh** — `POST /v1/oauth/{provider}/refresh {connection_id}`. Inspect the gateway log for the actual error.
2. **Refresh token expired?** — Most providers expire refresh tokens after 30-90 days of disuse. Solution: re-run the consent flow.
3. **Encryption key changed?** — If `PLINTH_OAUTH_ENCRYPTION_KEY` was rotated, existing connections become un-decryptable. Best practice: keep the old key around as `PLINTH_OAUTH_ENCRYPTION_KEY_PREVIOUS` and bulk-re-encrypt. v0.6 doesn't ship a built-in rotator — write a one-off script.
4. **Provider rate-limited the refresh endpoint?** — GitHub/Slack/Linear all rate-limit. Back off; retry.
5. **Scope mismatch?** — if the user revoked a scope at the provider, refresh succeeds but the access token is missing scopes. Re-run consent flow.

---

## 14. Performance Reference

### 14.1 Benchmark Suite Usage

```bash
make bench                                                # full suite, ~15 min
make bench-quick                                          # 100-RPS / 10-s sanity, ~1 min total
make bench-compare BASELINE=results/A.json LATEST=results/B.json
```

The harness is `plinth-bench` (under [`benchmarks/`](./benchmarks/)). Built on `httpx[http2]` + `asyncio` (no locust/k6 dependency). Output: per-workload JSON to `benchmarks/results/<workload>-<timestamp>.json`. Comparison output: markdown table.

Workloads (each ramps from 10 → 1000 RPS, captures p50/p95/p99/error_rate):

| Workload | What it exercises |
|---|---|
| `workspace_kv` | PUT/GET KV |
| `workspace_files` | PUT/GET file blobs |
| `workspace_snapshot` | snapshot capture |
| `gateway_invoke_cached` | cached `/v1/invoke` |
| `gateway_invoke_cold` | cold-cache `/v1/invoke` |
| `gateway_invoke_with_oauth` | `/v1/invoke` with OAuth-backed tool |
| `identity_token_issue` | `POST /v1/tokens` |

### 14.2 Reference Numbers

The README lists indicative numbers for a single MacBook M2 (10-core), `make serve` running all services on localhost ([`README.md`](./README.md) §Performance):

| Workload | RPS | p50 | p95 | p99 | error_rate |
|---|---:|---:|---:|---:|---:|
| `workspace_kv` | 500 | 4.2 ms | 18.7 ms | 42.1 ms | 0.5 % |
| `workspace_files` | 200 | 8.5 ms | 28.1 ms | 65.4 ms | 0.0 % |
| `workspace_snapshot` | 100 | 12.4 ms | 41.0 ms | 89.7 ms | 0.1 % |
| `gateway_invoke_cached` | 1000 | 2.1 ms | 9.4 ms | 22.0 ms | 0.0 % |
| `gateway_invoke_cold` | 300 | 14.8 ms | 52.6 ms | 110.3 ms | 0.2 % |
| `identity_token_issue` | 500 | 3.5 ms | 12.1 ms | 28.0 ms | 0.0 % |

> *TODO: confirm by running.* The README explicitly notes these numbers are placeholders pending a fresh benchmark run; commit fresh JSON to `benchmarks/results/baseline.json` and update the table in this doc + the README accordingly.

To reproduce:

```bash
make install
make serve
make bench
ls benchmarks/results/*.json
```

### 14.3 Tuning Guide

| Symptom | Knob | Direction |
|---|---|---|
| Gateway p95 spikes under load | `PLINTH_LOAD_SHED_MAX_INFLIGHT` | Raise (more concurrency) or lower (faster failure) |
| Worker pickup latency | `--poll-interval` (worker), `PLINTH_LEASE_REAPER_INTERVAL_SECONDS` | Lower both — but more polling traffic |
| Cache miss rate too high | Tool's `cache_ttl_seconds` | Raise per-tool TTL where staleness is acceptable |
| JWKS verification slow | First-call cold path: pre-warm with `verify_token` on boot | — |
| Postgres connection storms | `PLINTH_DB_POOL_MAX_SIZE` | Raise; ensure Postgres `max_connections` is higher |
| OTLP buffer backpressure | `PLINTH_OTLP_BATCH_SIZE`, `PLINTH_OTLP_FLUSH_INTERVAL_SECONDS` | Raise batch size; lower flush interval if collector is fast |
| High audit DB write IOPS | Move to Postgres; partition `audit_events` by month | — |

---

## 15. Contributing

### 15.1 Repo Layout

The repository is a single monorepo with one Python service per directory under `services/`, one MCP server per directory under `mcp-servers/`, two SDKs under `sdk/`, examples under `examples/`, and shared specs/docs at the top.

```
plinth/
├── services/                    # FastAPI services (workspace, gateway, identity, dashboard)
├── mcp-servers/                 # Real-API MCP servers (github, slack, linear)
├── mock-mcp-server/             # Offline 6-tool demo server
├── sdk/python/                  # plinth — canonical SDK
├── sdk/typescript/              # @plinth/sdk — parity SDK (ESM, Node 20+)
├── worker/                      # plinth-workflow-worker durable runner
├── benchmarks/                  # plinth-bench load harness
├── examples/                    # 5 runnable demos
├── docs/architecture/           # 9 component design docs
├── docs/adr/                    # 6 Architecture Decision Records
├── specs/openapi/               # OpenAPI 3.1 specs (workspace.yaml, gateway.yaml)
├── specs/schemas/               # JSON Schemas (events, capability tokens)
├── specs/proto/                 # Aspirational gRPC sketches
├── scripts/                     # spawn helpers, healthchecks, demo orchestration
├── Makefile                     # install / test / serve / demo* / bench
├── docker-compose.yml           # 8-service container stack
├── CONTRACTS.md                 # API source of truth — read this first
├── CONVENTIONS.md               # Style / layout / forbidden patterns
├── ARCHITECTURE.md              # 10-min architecture overview
├── README.md                    # Pitch + quickstart
├── ROADMAP.md                   # Versioned feature plan
├── CHANGELOG.md                 # Per-version diff
└── TECHNICAL_REFERENCE.md       # This file
```

### 15.2 Conventions

See [`CONVENTIONS.md`](./CONVENTIONS.md). Highlights:

- Python 3.11+, `black` (line 100), `ruff`, `mypy --strict` aspirationally.
- TypeScript 5.4+, ESM only, `prettier`, `eslint`.
- ID prefix scheme: `ws_`, `kv_`, `file_`, `snap_`, `br_`, `tool_`, `evt_`, `tx_`, `step_`, `wf_`, `tok_`, `conn_`, `worker_`, `txc_`. ULID format: 26 chars Crockford base32.
- Per-Python-service layout: `pyproject.toml`, `src/<package>/{__init__, __main__, api.py, models.py, settings.py, …}`, `tests/`.
- Every public function/class has a Google-style docstring.
- Every Python file: `# SPDX-License-Identifier: Apache-2.0` then `# Copyright 2026 The Plinth Authors`.
- Every test must run offline (no real network calls) — use `respx` or fixtures.
- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.
- README quickstart must work in CI on every PR.

### 15.3 Adding a Service — Checklist

1. Create `services/<name>/` with `pyproject.toml`, `src/plinth_<name>/`, `tests/`, `Dockerfile`, `README.md`, `migrations/0001_initial.sql`.
2. Wire `__main__.py` to the FastAPI app and `uvicorn.run`.
3. Add `settings.py` with `pydantic-settings`, prefix `PLINTH_<NAME>_`.
4. Add the service to `Makefile` (`install-<name>`, `test-<name>`, `serve-<name>`, `stop`).
5. Add the service to [`docker-compose.yml`](./docker-compose.yml).
6. Document the service in [`CONTRACTS.md`](./CONTRACTS.md), [`README.md`](./README.md) §Status table, and a new section in this Technical Reference.
7. Add an OpenAPI spec under `specs/openapi/<name>.yaml` if the service has external endpoints.
8. Tests: ≥ 80 % coverage from day one.

### 15.4 Adding an MCP Server — Checklist

1. Pick a slug (e.g. `notion`).
2. `mcp-servers/<slug>/` with `pyproject.toml`, `src/<slug>_mcp/`, `tests/` (use `respx` to mock the provider).
3. Implement `__main__.py` exposing `GET /healthz`, `GET /tools`, `POST /invoke/{tool_id}`.
4. Read `Authorization` header, forward as-is to the provider's API.
5. Map provider errors to Plinth error envelopes (`{"error":{"code":"...", "message":"..."}}`).
6. Register the server in `docker-compose.yml`, `Makefile`, and `.claude/launch.json`.
7. Add OAuth provider config to the gateway if needed (see [§8.6](#86-adding-a-new-provider-step-by-step)).
8. Document tools in [`CONTRACTS.md`](./CONTRACTS.md) §"<provider> MCP server".

### 15.5 Adding a Demo — Checklist

1. `examples/<NN>-<name>/` with `pyproject.toml`, `agent.py` or equivalent entry-point, `README.md`.
2. Use only the SDK; no direct HTTP calls.
3. Provide a simulation mode that runs offline.
4. Add a `make demo-<name>` target that runs the demo end-to-end.
5. Verify the example installs via `make install-examples`.
6. Document expected output in the README.
7. Add tests if non-trivial (state machine demos especially).

### 15.6 Adding a Migration — Checklist

1. Generate the file: `python -m plinth_<svc> migrate --create "add foo column"` produces `services/<svc>/migrations/000<N>_add_foo_column.sql`.
2. Write the forward DDL. Use `IF NOT EXISTS` only for additions; for type changes write the full transformation.
3. Write the rollback DDL in `000<N>_add_foo_column_rollback.sql` if the change is reversible. **Required** for type changes, irreversible drops, and changes that backfill data.
4. Update Pydantic models if the schema change affects the API surface.
5. Add tests under `tests/test_migrations.py` exercising the new migration on a fresh DB.
6. Record the change in [`CHANGELOG.md`](./CHANGELOG.md).
7. Verify on Postgres locally if possible (`PLINTH_TEST_POSTGRES_URL=postgresql://...`).

### 15.7 Test Discipline

Per-suite expectations:

| Suite | Min coverage | Fixture strategy |
|---|---:|---|
| `services/workspace`, `gateway`, `identity` | 90 % | `httpx.AsyncClient` against the FastAPI app, fresh tmp-path SQLite per test |
| `services/dashboard` | 80 % | `respx` against upstream services |
| `mock-mcp-server`, MCP servers | ≥ 90 % | `respx` against provider APIs |
| `sdk/python`, `sdk/typescript` | ≥ 90 % | mock HTTP layer, no real services |
| `worker` | ≥ 80 % | testcontainers-style spinning a real workspace |
| `benchmarks` | ≥ 80 % | unit tests on the harness logic |

Tests must run offline. If a test needs an external service, gate it behind an env var (`PLINTH_TEST_POSTGRES_URL`, `PLINTH_TEST_GITHUB_TOKEN`) and skip otherwise.

---

## 16. Version History (Compact)

Per-version theme + counts. Source: [`CHANGELOG.md`](./CHANGELOG.md).

| Version | Date | Theme | Tests passing | Major addition |
|---|---|---|---:|---|
| 0.1.0 | 2026-05-05 | PoC | 358 (330 Py + 28 TS) | Workspace + Gateway, mock MCP, research-agent demo (71 % token saving) |
| 0.2.0 | 2026-05-06 | MVP | 558 (530 Py + 28 TS) | Channels, Workflows, Rate limits + cost caps, Dashboard, demos 02 + 03 |
| 0.3.0 | 2026-05-06 | Production-credible | 814 (735 Py + 79 TS) | Identity service + JWT, multi-tenancy, GitHub OAuth + MCP, demo 04, TS SDK parity |
| 0.4.0 | 2026-05-07 | Scale & operability | 1084 (996 Py + 88 TS) | Postgres backend, OTLP export, RS256 + key rotation, Slack + Linear OAuth + MCP, GC + retention |
| 0.5.0 | 2026-05-07 | Reliability & coordination | 1373 (1274 Py + 99 TS) | Migrations, durable workflow executor + workers, Saga transactions, typed channels + DLQ, benchmarks + load-shedding, demo 05 |
| **0.6.0** | **2026-05-07** | **Distribution & polish** | **1621 (1503 Py + 118 TS)** | **Federated revocation, Postgres advisory locks, migration rollback, generic resource locks, channel-schema migration helpers, workflow viz** |

---

## 17. Glossary

| Term | Definition |
|---|---|
| **Agent** | A first-class principal in Plinth — a software entity with its own identity, scopes, and audit trail. The unit of capability-token issuance. |
| **Audit event** | An immutable row in `audit_events` recording one tool invocation. Every `/v1/invoke` writes one synchronously. |
| **Branch** | A writable workspace fork from a snapshot. Reads see branch-specific writes first, then fall through to the source snapshot. |
| **Capability token** | A short-lived signed JWT that explicitly enumerates what an agent may do (scopes). Issued by Identity, verified at Workspace + Gateway. |
| **Channel** | A workspace-scoped, lazily-created, monotonically-sequenced durable message queue. Used for multi-agent handoffs. |
| **Compensation** | The "undo" tool call registered alongside a transaction call, run in reverse order if a later call fails. Saga-pattern transactions. |
| **Consumer cursor** | A per-consumer pointer into a channel's sequence. Lets multiple readers track their own progress over the same log. |
| **DLQ** | Dead-letter queue — a hidden `<channel>.deadletter` sub-channel where messages that fail JSON-Schema validation are routed. |
| **Drain** | A worker's graceful-shutdown signal. After draining, it acquires no new leases but finishes in-flight steps. |
| **Federated revocation** | The v0.6 mechanism by which a `revoke` on one Identity replica propagates to all Workspace + Gateway verifiers via cursor-paginated polling. |
| **Gateway** | The Plinth service that proxies every external tool call. Owns auth, cache, audit, rate limits, OAuth, and OTLP export. |
| **GC** | Garbage collection — the per-workspace sweeper that deletes versions / snapshots / blobs not retained by any policy or referenced by any snapshot. |
| **Identity** | The Plinth service that issues + verifies + revokes JWT capability tokens, manages tenants, and rotates RS256 signing keys. |
| **JWKS** | JSON Web Key Set — the public-key metadata Identity publishes at `/v1/.well-known/jwks.json` so verifiers can validate RS256 tokens without a network round-trip per request. |
| **Lease** | A time-bounded ownership claim a worker holds on a workflow step. Heartbeats extend it; expiry frees the step for another worker. |
| **Lock** | A generic named resource lock (v0.6). Race-safe upsert with TTL + heartbeats; used for inter-agent mutual exclusion. |
| **MCP** | Model Context Protocol — Anthropic's open tool-exposure standard. Plinth's gateway is an MCP client; backend tools are MCP servers. |
| **OTLP** | OpenTelemetry Protocol. The gateway optionally emits each audit event as an OTLP Log to a configured collector. |
| **PKCE** | Proof Key for Code Exchange — the OAuth 2.0 extension that prevents authorization-code interception. Plinth's OAuth client implementation is PKCE-correct for all 3 providers. |
| **Snapshot** | An immutable, named point-in-time capture of every key + path version on a timeline. Cheap (metadata only). Used for resumability and audit. |
| **Tenant** | An isolation boundary above workspaces. Realised in v0.6 as a `tenant_id` column on every state-bearing table; tokens carry `tenant_id` claim. |
| **Tombstone** | A KV/file delete that writes a new version with `deleted=1`, preserving history. |
| **Transaction** | A sequence of gateway tool calls grouped as a unit, with optional per-call compensations executed in reverse on failure. Saga pattern. |
| **Workflow** | A manifest of expected step names plus a log of completed/failed/cancelled steps. Resumable from the most recent snapshot. |
| **Worker** | A long-running process (`plinth-workflow-worker`) that polls workspaces, leases pending workflow steps, and executes registered handlers. |
| **Workspace** | The top-level isolation boundary for an agent's state — KV + files + snapshots + branches + channels + workflows + locks. |

---

## 18. Reference Links

| Resource | Path / URL |
|---|---|
| Repository | https://github.com/nico-schindlbeck-jpg/plinth |
| Architecture overview | [`ARCHITECTURE.md`](./ARCHITECTURE.md) |
| API source of truth | [`CONTRACTS.md`](./CONTRACTS.md) |
| Conventions | [`CONVENTIONS.md`](./CONVENTIONS.md) |
| Roadmap | [`ROADMAP.md`](./ROADMAP.md) |
| Per-version diffs | [`CHANGELOG.md`](./CHANGELOG.md) |
| Component designs | [`docs/architecture/01-system-overview.md`](./docs/architecture/01-system-overview.md) through [`docs/architecture/09-rate-limiting-design.md`](./docs/architecture/09-rate-limiting-design.md) |
| ADR 0001 — language and stack | [`docs/adr/0001-language-and-stack.md`](./docs/adr/0001-language-and-stack.md) |
| ADR 0002 — storage decisions | [`docs/adr/0002-storage-postgres-and-objectstore.md`](./docs/adr/0002-storage-postgres-and-objectstore.md) |
| ADR 0003 — MCP as the tool protocol | [`docs/adr/0003-mcp-as-tool-protocol.md`](./docs/adr/0003-mcp-as-tool-protocol.md) |
| ADR 0004 — Temporal vs custom workflow | [`docs/adr/0004-temporal-vs-custom-workflow.md`](./docs/adr/0004-temporal-vs-custom-workflow.md) |
| ADR 0005 — licensing posture | [`docs/adr/0005-bsl-vs-apache-licensing.md`](./docs/adr/0005-bsl-vs-apache-licensing.md) |
| ADR 0006 — multi-tenancy model | [`docs/adr/0006-multitenancy-model.md`](./docs/adr/0006-multitenancy-model.md) |
| OpenAPI — Workspace | [`specs/openapi/workspace.yaml`](./specs/openapi/workspace.yaml) |
| OpenAPI — Gateway | [`specs/openapi/gateway.yaml`](./specs/openapi/gateway.yaml) |
| Event schema (OTLP) | [`specs/schemas/event.schema.json`](./specs/schemas/event.schema.json) |
| Capability-token schema | [`specs/schemas/capability-token.schema.json`](./specs/schemas/capability-token.schema.json) |
| Mermaid diagrams | [`specs/diagrams/`](./specs/diagrams/) |
| Demo 01 — research-agent | [`examples/01-research-agent/`](./examples/01-research-agent/) |
| Demo 02 — multi-agent handoff | [`examples/02-multi-agent-handoff/`](./examples/02-multi-agent-handoff/) |
| Demo 03 — resumable workflow | [`examples/03-resumable-workflow/`](./examples/03-resumable-workflow/) |
| Demo 04 — GitHub issue triage | [`examples/04-github-issue-triage/`](./examples/04-github-issue-triage/) |
| Demo 05 — durable workflow | [`examples/05-durable-workflow/`](./examples/05-durable-workflow/) |
| Worker CLI | [`worker/`](./worker/) |
| Benchmark harness | [`benchmarks/`](./benchmarks/) |
| License | [`LICENSE`](./LICENSE) |
