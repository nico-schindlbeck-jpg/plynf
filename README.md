<div align="center">

# 🪨 Plinth

**The substrate where production agents actually work.**

*A versioned workspace + tool gateway + observability plane — designed for AI agents, not retrofitted from human UIs.*

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Status](https://img.shields.io/badge/status-v1.2%20+%20LLM%20layer-brightgreen.svg)](#status)
[![Tests](https://img.shields.io/badge/tests-2579%20passing-brightgreen.svg)](#status)
[![API](https://img.shields.io/badge/API-v1%20stable-blue.svg)](docs/API_STABILITY.md)
[![Coordination](https://img.shields.io/badge/coordination-Redis%20opt--in-orange.svg)](#status)

</div>

---

## Why Plinth?

Today's AI agents are wrapped around interfaces designed for humans — clicking buttons, parsing screenshots, re-reading the same content from chat history. This is **slow, expensive, and brittle**.

Plinth flips the model: **the agent is the first-class user**. We give it:

- **A persistent, versioned workspace** — files, structured KV, snapshots, branches. The agent's memory survives crashes, restarts, and hand-offs.
- **A semantic tool gateway** — one auth boundary for every MCP / REST / GraphQL tool, with caching, idempotency, dry-run, audit, rate limits, cost caps, and real OAuth flows for **GitHub, Slack, and Linear**.
- **Channels for multi-agent handoffs** — durable, sequenced message queues so agents can compose into pipelines without inheriting each other's prompt context.
- **Workflows with resume** — checkpointed step sequences. Crash mid-flight, restart, pick up from the last snapshot.
- **An identity service with JWT capability tokens** — RS256 with automatic key rotation (v0.4) or HS256, agent-scoped, scope-grammar, revocable, multi-tenant.
- **A web dashboard** — real-time view of workspaces, audit log, cache hits, cost rollups, **OTLP status, time-series graphs** (v0.4).
- **Production storage backends** — SQLite for development, **Postgres for scale** (v0.4). Workspace **GC + retention policies** for operational hygiene.
- **OpenTelemetry observability** — emit every tool invocation as an OTLP log to Datadog, Tempo, Honeycomb, or any OTLP collector. (v0.4)
- **Token economics that actually work** — caching at the gateway and structured state in the workspace cuts agent context usage by 50–70 % on real workloads.

> **Headline result from `examples/01-research-agent/`**: A 5-source research-and-report task uses **71 % fewer tokens** with Plinth than without (measured across 3 topics, full stack with all services running). See [the demo](#headline-demo) below.
>
> **Multi-agent demo** (`examples/02-multi-agent-handoff/`): three agents collaborate via Plinth channels in 8.7k total tokens.
>
> **Resumable workflow** (`examples/03-resumable-workflow/`): a 6-step pipeline crashes mid-flight; resume saves **32 %** of the work versus restart-from-scratch.

## Status

This is **v0.6 distribution & polish** — adds federated revocation across multi-node Identity, Postgres advisory locks, migration rollback execution, generic resource locks, channel-schema migration helpers, and a visual workflow graph in the Dashboard. On top of v0.5 reliability.

**2066 tests passing** (1901 Python + 136 TS SDK + 29 TS worker). 15 Postgres tests skipped (require running Postgres). Backwards-compatible with every v0.1–v0.6 demo and deployment.

**v1.0 ships GA**: stable API guarantees (`docs/API_STABILITY.md`), per-tenant resource quotas, GDPR export + delete, tamper-evident audit chain, multi-region scaffolding, unified `plinth` CLI, Prometheus on every service, dashboard time-series, formal SLOs, k8s + Helm + Terraform deployment artifacts, threat model.

| Component | Port | State |
|-----------|-----:|-------|
| Workspace service (KV + files + snapshots + branches + channels + workflows + tenants + **GC** + **Postgres**) | 7421 | ✅ Working |
| Tool gateway (MCP proxy + caching + audit + rate-limits + cost-caps + OAuth + **OTLP** + **Postgres**) | 7422 | ✅ Working |
| Mock MCP server (6 tools, offline-capable) | 7423 | ✅ Working |
| Web dashboard (SPA + proxies + **time-series graph** + **OTLP status**) | 7424 | ✅ Working |
| Identity service (JWT, tenants, JWKS, **RS256 + key rotation**, **Postgres**) | 7425 | ✅ Working |
| GitHub MCP server (7 tools, real GitHub API) | 7426 | ✅ Working |
| **Slack MCP server** (4 tools, real Slack API) | 7427 | ✅ Working (v0.4) |
| **Linear MCP server** (5 tools, GraphQL) | 7428 | ✅ Working (v0.4) |
| Python SDK (full surface, identity keys, channels, workflows) | — | ✅ Working |
| TypeScript SDK at parity (full surface, identity keys, token counting) | — | ✅ Working |
| Demo 01 — research-agent (71 % token saving) | — | ✅ Working |
| Demo 02 — multi-agent handoff (channels) | — | ✅ Working |
| Demo 03 — resumable workflow (crash + resume) | — | ✅ Working |
| Demo 04 — GitHub issue triage (OAuth) | — | ✅ Working |
| Docker Compose (8 services) | — | ✅ Working |
| ADRs + OpenAPI specs | — | ✅ Documented |
| **Migration framework** (versioned SQL + CLI + admin endpoints) | — | ✅ Working (v0.5) |
| **Durable workflow executor** + lease-based recovery | — | ✅ Working (v0.5) |
| **Workflow transactions** with Saga-style compensating actions | — | ✅ Working (v0.5) |
| **Typed channels + dead-letter queue** | — | ✅ Working (v0.5) |
| **Stress benchmarks + load-shedding middleware** | — | ✅ Working (v0.5) |
| Demo 05 — durable workflow with crash recovery (NEW v0.5) | — | ✅ Working |
| Federated revocation, durable workflow engine UI | — | ❌ v0.6 |

## Quickstart

```bash
# 1. Clone & enter
git clone https://github.com/your-org/plinth.git
cd plinth

# 2. Install everything (6 services + Python SDK + TS SDK + 4 examples)
make install

# 3. Run all tests (1274 Python tests across 11 suites)
make test

# 4. Start all 8 services
make serve     # workspace + gateway + mock-mcp + dashboard + identity + github-mcp + slack-mcp + linear-mcp

# 5. Run the four demos
make demo            # Demo 01 — token-comparison (71% saving)
make demo-handoff    # Demo 02 — multi-agent handoff (Researcher → Writer → Reviewer)
make demo-resume     # Demo 03 — crash-and-resume
make demo-triage     # Demo 04 — GitHub issue triage (OAuth-backed; simulation mode by default)
# Demo 05 — durable-workflow with crash recovery (separate workflow)
# In one terminal:  plinth-workflow-worker --handlers-module handlers --concurrency 2
# In another:       python examples/05-durable-workflow/start_workflow.py --topic "renewable energy"

# 6. Open the dashboard
open http://localhost:7424/

# 7. Stop services when done
make stop
```

After step 5 you should see something like:

```
═══════════════════════════════════════════════════════════════════
  TOKEN-USAGE COMPARISON — research-agent on topic "renewable energy"
═══════════════════════════════════════════════════════════════════
  Baseline (no Plinth):        23,704 tokens   |   $0.0810
  With Plinth:                  6,795 tokens   |   $0.0345
  ─────────────────────────────────────────────
  Reduction:                     71.3 %        |   $0.0464 saved
═══════════════════════════════════════════════════════════════════
  Wall-clock time:        Baseline   0.1 s   |   Plinth   0.2 s
  Tool calls:             Baseline     6   |   Plinth     6   (cached on second run)
═══════════════════════════════════════════════════════════════════
  Mode: simulation | Topic: renewable energy
  Baseline LLM calls: 8 | Plinth LLM calls: 7
═══════════════════════════════════════════════════════════════════
```

Measured across all three bundled topics:

| Topic | Baseline | Plinth | Reduction |
|-------|---------:|-------:|----------:|
| renewable energy | 23,704 tokens | 6,795 tokens | **71.3 %** |
| ai agents | 25,329 tokens | 7,092 tokens | **72.0 %** |
| climate policy | 26,292 tokens | 7,601 tokens | **71.1 %** |

Token counts are exact (cl100k_base via tiktoken). Cost estimates use Anthropic Sonnet pricing ($3/M input, $15/M output).

## Performance (v1.1)

Measured on a single MacBook (Apple M4, 10-core, 16 GB RAM, macOS 25.0.0, Python 3.9.6) with `make serve` running all services on localhost. Each workload ramps from 10 → 500 RPS over 10 s, holds at 500 RPS for 20 s, then cools down for 5 s (35 s total per workload). Cold start: data dir wiped before the run. Gateway rate limiting disabled (`PLINTH_RATE_LIMITS_ENABLED=false`) for stable saturation. The full machine-readable run lives in `benchmarks/results/baseline-v1.1.json`.

| Workload              | RPS  | p50       | p95        | p99        | error_rate |
|-----------------------|-----:|----------:|-----------:|-----------:|-----------:|
| workspace_kv          |  500 |   8.34 ms |   39.41 ms |  113.36 ms |       0.00 % |
| workspace_files       |  500 |   8.09 ms |   38.60 ms |  181.48 ms |       0.00 % |
| workspace_snapshot    |  500 | 998.41 ms | 1347.30 ms | 1677.29 ms |       0.01 % |
| gateway_invoke_cached |  500 | 203.10 ms |  294.54 ms |  320.88 ms |       0.00 % |
| gateway_invoke_cold   |  500 | 491.42 ms |  766.80 ms |  945.62 ms |       0.00 % |
| identity_token_issue  |  500 |   8.73 ms |   12.91 ms |   31.48 ms |       0.00 % |

> Run captured at `2026-05-10T11:44:39Z` from git `912bfa5` (services v1.1.0). Single-uvicorn-worker localhost saturation — comparative trends across versions on the same hardware, **not** absolute production-grade numbers. For production targets see [`docs/slos.md`](docs/slos.md). Workspace KV/files and identity hot paths comfortably handle 500 RPS in single-digit ms; `workspace_snapshot` (PUT + create-snapshot per request) and `gateway_invoke_*` (full proxy → mock-mcp roundtrip) saturate well below 500 RPS at this concurrency, which is why their tail latencies dominate the table.

Reproduce: `make bench`, or `make bench-quick` for a 100-RPS / 10-second sanity sweep. The harness lives in `benchmarks/` and is built on `httpx[http2]` + `asyncio` (no locust/k6 dependency). It also pairs with the **load-shedding middleware** (workspace + gateway) so a service over its `PLINTH_LOAD_SHED_MAX_INFLIGHT` cap returns `503 Retry-After` instead of grinding to a halt.

## Architecture

```
                        ┌──────────────────────────┐
                        │       Your Agent         │
                        │   (Python or TS SDK)     │
                        └──┬─────────────┬─────────┘
                           │             │  Authorization: Bearer <JWT>
              ┌────────────▼─────┐ ┌─────▼─────────┐ ┌───────────────┐
              │  Workspace       │ │ Tool Gateway  │ │ Identity      │
              │  :7421           │ │ :7422         │ │ :7425  (v0.3) │
              │                  │ │               │ │               │
              │ • KV + Files     │ │ • MCP Proxy   │ │ • Issue JWT   │
              │ • Versioning     │ │ • Caching     │ │ • Verify      │
              │ • Snapshots      │ │ • Audit log   │ │ • Revoke      │
              │ • Branches       │ │ • Rate limits │ │ • Tenants     │
              │ • Channels       │ │ • Cost caps   │ │ • JWKS        │
              │ • Workflows      │ │ • OAuth (PKCE)│ └───────────────┘
              │ • Tenants  (v0.3)│ │ • Tenants     │
              └────────┬─────────┘ └───┬───────────┘
                       │               │ proxies + auth
                ┌──────▼────┐   ┌──────▼─────────┐  ┌──────────────┐
                │ SQLite +  │   │  Mock MCP :7423│  │ GitHub MCP   │
                │ blobs     │   │  6 tools       │  │ :7426 (v0.3) │
                └───────────┘   └────────────────┘  │ 7 GitHub tools│
                                                    └──────────────┘

                                ┌────────────────────────────┐
                                │  Dashboard :7424           │
                                │  Read-only SPA + proxy     │
                                │  Workspaces, audit, costs  │
                                └────────────────────────────┘
```

See [ARCHITECTURE.md](./ARCHITECTURE.md) for a deeper walk-through and [docs/architecture/](./docs/architecture/) for component-level designs.

## What's in the box?

```
plinth/
├── services/
│   ├── workspace/          # KV + files + snapshots + branches + channels + workflows + tenants + GC
│   ├── gateway/            # MCP proxy + caching + audit + rate-limits + cost-caps + OAuth + OTLP
│   ├── dashboard/          # Read-only web SPA + time-series graph (v0.4)
│   └── identity/           # JWT capability tokens + tenants + RS256 + key rotation (v0.4)
├── mcp-servers/
│   ├── github/             # 7 real GitHub tools (v0.3)
│   ├── slack/              # 4 real Slack tools (v0.4)
│   └── linear/             # 5 real Linear tools, GraphQL (v0.4)
├── sdk/
│   ├── python/             # plinth — full SDK incl. identity + signing keys
│   └── typescript/         # @plinth/sdk — full parity with Python
├── examples/
│   ├── 01-research-agent/         # Token-comparison (71% reduction)
│   ├── 02-multi-agent-handoff/    # Channels-based pipeline
│   ├── 03-resumable-workflow/     # Crash + resume
│   └── 04-github-issue-triage/    # OAuth-backed GitHub agent
├── mock-mcp-server/        # 6 demo tools, 15 fixtures (offline-capable)
├── specs/
│   ├── openapi/            # Workspace + Gateway OpenAPI 3.1 specs
│   ├── proto/              # gRPC sketches
│   ├── schemas/            # JSON Schemas (events, capability tokens)
│   └── diagrams/           # Mermaid sequence + state + flow diagrams
├── docs/
│   ├── architecture/       # 6 per-component design docs
│   └── adr/                # 6 Architecture Decision Records
├── .claude/launch.json     # preview_start config — 8 servers
├── docker-compose.yml      # 8-service stack
├── Makefile                # install / test / serve / demo[-handoff|-resume|-triage] / stop
├── CONTRACTS.md            # API surface — single source of truth
└── CONVENTIONS.md          # Code style, patterns, naming
```

### Storage drivers

By default everything runs against SQLite — zero ops, perfect for dev. For production:

```bash
PLINTH_STORAGE_DRIVER=postgres
PLINTH_DATABASE_URL=postgresql://user:pw@host:5432/plinth
# Or per-service:
PLINTH_WORKSPACE_DATABASE_URL=postgresql://...
PLINTH_GATEWAY_DATABASE_URL=postgresql://...
PLINTH_IDENTITY_DATABASE_URL=postgresql://...
```

### Observability

Set `PLINTH_OTLP_ENABLED=true` and `PLINTH_OTLP_ENDPOINT=http://your-collector:4318` on the gateway, and every tool invocation is emitted as an OpenTelemetry log to your favourite backend. The dashboard at `:7424` shows a live 60-minute graph derived from the same audit data.

### Auth

`PLINTH_IDENTITY_JWT_ALG=RS256` for production: the identity service generates an RSA-2048 keypair, encrypts the private key at rest with AES-GCM, publishes JWKS, and rotates the key every 30 days. Workspace and gateway fetch JWKS lazily and cache for 5 minutes; rotation is invisible to clients.

## Headline Demo

The `01-research-agent` example shows where Plinth's token savings come from:

**Baseline agent** (no Plinth):
1. Receives the topic
2. Searches the web (1 tool call)
3. For each of 5 sources: fetches full content, sends back to LLM with full conversation history
4. Re-reads sources at every reasoning step (because the chat history is the only memory)
5. Each subsequent step balloons the context

**Plinth agent**:
1. Receives the topic, creates/opens workspace
2. Searches once (gateway-cached if repeated)
3. Fetches each source — gateway caches; identical fetches are free
4. Stores extracted facts in workspace KV (structured, addressable)
5. Reasoning steps reference workspace by **key**, not by re-reading content into the prompt
6. Final report writes to workspace files; snapshot for audit

Run it yourself:
```bash
cd examples/01-research-agent
python compare.py --topic "renewable energy"
```

## Contributing

We welcome contributions! See [CONTRIBUTING.md](./CONTRIBUTING.md). Good first PRs:
- Fix a flaky test
- Add a new tool to the mock MCP server
- Implement an additional example agent
- Improve an ADR

## License

Apache 2.0 — see [LICENSE](./LICENSE). Note: a production hosted runtime would likely move to BSL; see [ADR 0005](./docs/adr/0005-bsl-vs-apache-licensing.md) for the rationale.

## The bigger picture

Plinth is a PoC of a thesis: **the next infrastructure layer is the agent-native substrate** — analogous to what AWS is for servers, Stripe for payments, Vercel for frontends. The PoC proves the primitives. The platform comes next.

For the longer-form thinking behind this, see [docs/why-plinth.md](./docs/why-plinth.md).
