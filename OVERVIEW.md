# Plinth — Overview

*A two-page intro for anyone who wants to understand what Plinth is, what state it's in, and how it works — without needing to read code.*

---

## TL;DR

**Plinth is the runtime where production AI agents actually work.** Today's AI agents are wrapped around interfaces designed for humans — clicking buttons, parsing screenshots, re-reading the same content from chat history every step. That's slow, expensive, and brittle. Plinth flips the model: the **agent** becomes the first-class user of dedicated infrastructure.

**Six things it gives every agent that runs on it:**

1. **A persistent workspace** — like a private filesystem and database that survives crashes, restarts, and hand-offs to other agents.
2. **A unified tool gateway** — one connection point for every external tool the agent might use (Slack, GitHub, Linear, internal APIs). Caches results, enforces rate limits, cost caps, and audit trails automatically.
3. **Multi-agent coordination** — agents pass structured messages through durable channels instead of cramming everything into chat context.
4. **Resumable workflows** — when an agent crashes mid-task, another agent picks up exactly where it left off. No work lost.
5. **Production safety** — JWT-based authentication, per-agent rate limits, cost ceilings, atomic transactions with rollback, schema validation, dead-letter queues.
6. **Observability** — every action is auditable, every cost is attributable, performance is benchmarked.

**Headline result:** A typical 5-source research task uses **71 % fewer tokens** with Plinth than without — measured precisely, reproducible offline, three different topics. That's not a theoretical claim, it's in the repo as a runnable demo.

---

## Current build status

| | |
|--|--|
| **Latest milestone** | **v1.2.1 — LLM Layer parity (Python + TypeScript)** *(May 10, 2026)* |
| **Repo size** | ~128 000 lines of source + docs |
| **Tests passing** | **2 629** *(2 406 Python + 194 TS SDK + 29 TS Worker)* |
| **API stability** | `v1` stable — see `docs/API_STABILITY.md` |
| **Coordination** | memory (default) or Redis (cluster-shared) — opt-in via `PLINTH_COORDINATION_BACKEND=redis` |
| **OAuth providers** | GitHub, Slack, Linear, Notion, Google Workspace |
| **Deployable units** | 9 *(4 services, 3 real MCP tool servers, 1 mock tool server, 1 worker, 1 benchmark suite)* |
| **End-to-end demos** | 5 *(all run from a clean clone)* |
| **License** | Apache 2.0 |

### Roadmap at a glance

| Version | Theme | Status |
|--------:|-------|:------:|
| v0.1 | Proof-of-concept (workspace + tool gateway + headline demo) | done |
| v0.2 | MVP (channels + workflows + dashboard + production-safety knobs) | done |
| v0.3 | Production-credible (real auth, multi-tenancy, real GitHub OAuth, TS-SDK parity) | done |
| v0.4 | Scale & operability (Postgres backend, OpenTelemetry export, RS256 keys with rotation, more OAuth providers) | done |
| v0.5 | Reliability & coordination depth (migrations, durable workers, transactions, typed channels, benchmarks) | done |
| v0.6 | Distribution & polish (federated revocation, migration rollback, workflow viz, generic locks, schema-migration helpers, TS worker) | done |
| **v1.0** | **General Availability (per-tenant quotas, multi-region scaffolding, GDPR export+delete, tamper-evident audit, unified CLI, k8s/Helm/Terraform, Prometheus on every service, formal SLOs, threat model, stable API)** | **CURRENT** |
| post-1.0 | Continuous improvement (OTel migration, federated revocation, advisory locks, enterprise SSO, compliance certifications) | next |

### Backwards compatibility

Every demo from v0.1 forward still runs unchanged. New features are opt-in by default. **API v1 is now stable** — additive-only changes guaranteed for ≥12 months; breaking changes require `/v2/` namespace with deprecation headers + migration guide.

---

## How it works — functional flow, plain language

### The mental model

Imagine you've hired a freelance researcher to write you a report. Two ways to manage them:

**Way A (today's typical agent):** You're on a phone call with them the whole time. Every time they want to remember something, they have to repeat it back to you in the conversation. Every time they pick up a new source, they read it aloud again. Every step, you're paying for the full conversation history. By hour three, the call costs are insane and they've forgotten what was decided in hour one.

**Way B (Plinth):** They have a desk. They put sources on the desk. They write notes in a notebook. When they want to refer to source 3, they pick it up off the desk — they don't read it aloud again. When they finish for the day, the desk is still there tomorrow. If they get sick, a colleague can sit at the same desk and pick up exactly where they left off.

Plinth is the desk + notebook + filing cabinet for AI agents.

### What happens when an agent runs

Six things happen behind the scenes for every meaningful agent action:

1. **The agent calls the SDK** (Python or TypeScript). It says things like *"store this fact under key X"* or *"call this external tool with these arguments."*
2. **The SDK talks to the right backend service.** Workspace state goes to the **Workspace** service (port 7421). Tool calls go to the **Tool Gateway** (port 7422).
3. **The Workspace stores the value with a version number.** Old versions don't get overwritten — they're kept, so the agent can roll back, compare, or branch off.
4. **The Tool Gateway** is smarter than a plain proxy:
   - If the same tool was called with the same arguments recently → returns the cached result instantly (zero latency, zero cost).
   - Otherwise → applies authentication (OAuth tokens for GitHub, etc.), checks the agent's rate limit and cost ceiling, then calls the actual external tool.
   - Records a complete audit entry: what was called, with what arguments, by which agent, in which tenant, what it cost, what came back.
5. **Multi-agent coordination** uses a separate primitive called *channels*. When agent A finishes its part, it drops a typed message in a channel. Agent B picks it up — but only that message, not the entire conversation history that produced it. Token-efficient by construction.
6. **Workflows** wrap longer tasks. Each "step" is checkpointed. Workers pull pending steps, execute them, write results back. If a worker dies mid-step, another worker takes over after a short timeout.

The user sees: *the agent did its task.* What they don't see: a workspace was created, sources were cached, audit events were written, costs were tracked, and at every step the system was ready to recover from failure.

### What it costs (token economics)

The whole platform exists because of one number: **agent context size grows quadratically with steps if you do it naively.** Each new reasoning step inflates the prompt with everything that came before. By step ten, you're paying ~10× more per token than at step one.

Plinth structurally prevents that. State lives in the workspace, referenced by key. Each reasoning step gets a focused, small prompt. The token bill stays *linear* in actual reasoning work, not quadratic in conversation history.

Measured across three real research topics:

| Topic | Without Plinth | With Plinth | Saved |
|-------|---------------:|------------:|------:|
| renewable energy | 23 704 tokens / $0.0810 | 6 795 tokens / $0.0345 | **71.3 %** |
| ai agents | 25 329 / $0.0858 | 7 092 / $0.0348 | **72.0 %** |
| climate policy | 26 292 / $0.0884 | 7 601 / $0.0376 | **71.1 %** |

Pricing reference: Anthropic Sonnet ($3 per million input tokens, $15 per million output).

---

## What you can do today (no setup beyond `make install`)

```bash
make install        # one-time: services + Python SDK + TypeScript SDK + 5 examples
make test           # 1 274 Python tests run in ~30 seconds
make serve          # all services start in the background
make demo           # 71 % token-saving demo runs offline
make demo-handoff   # three agents collaborate via channels
make demo-resume    # crash a workflow, watch it recover
make demo-triage    # GitHub-issue triage agent (simulation mode, no GitHub account needed)
open http://localhost:7424/   # web dashboard with live audit, costs, and DLQ counts
```

---

## What it is *not*

- **Not a model.** Plinth is model-agnostic. You bring your own (Anthropic, OpenAI, local). It just handles everything around it.
- **Not an agent framework.** It doesn't tell you how to write an agent's reasoning loop. It gives you the substrate the agent runs on.
- **Not a single point of failure.** Workspace, Gateway, Identity, MCP servers, Workers all run as separate processes. Any can be replicated, replaced, or scaled independently.
- **Not finished.** v0.5 is a strong reliability foundation. v0.6 adds federation, dashboards for workflows, and a few quality-of-life polish items. v1.0 is when we ship guarantees and certifications.

---

## Where to dig in next

| If you want to… | Read |
|-----------------|------|
| **Run it yourself** | `README.md` (top-level) — five-minute quickstart |
| **Understand the architecture** | `ARCHITECTURE.md` — 10-minute walk-through of how the pieces fit |
| **See the strategic thesis** | `docs/why-plinth.md` |
| **Understand the API surface** | `CONTRACTS.md` — single source of truth for every endpoint |
| **Read the design decisions** | `docs/adr/` — six Architecture Decision Records explaining why we chose what we chose |
| **Check what's coming** | `ROADMAP.md` |
| **See the version history** | `CHANGELOG.md` |
