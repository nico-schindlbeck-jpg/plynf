<div align="center">

# Plinth — Executive Summary

**The runtime where production AI agents actually work.**

`v0.6 — Distribution & Polish` · May 2026 · Apache 2.0

</div>

---

## 1. The 30-Second Pitch

**Plinth is the operating layer for AI agents — the equivalent of what AWS is to servers, what Stripe is to payments, what Vercel is to websites.** Today's agents are bolted onto interfaces designed for humans, which makes them slow, unreliable, and grossly expensive to run at scale. Plinth replaces that with a purpose-built substrate: a persistent workspace, a single tool gateway, and the coordination machinery teams need to run agents in production. The result, demonstrated and reproducible, is **71% fewer tokens** on real research workloads — without changing the underlying model.

---

## 2. The Problem We Solve

Anyone shipping AI agents in 2026 has met the same wall. The model is fine. The agent logic is fine. But every step the agent takes drags the entire conversation history with it — every web page it read, every tool it called, every intermediate thought. By step ten, the prompt is enormous and most of what the agent is "paying to think about" is just re-reading its own past notes. Costs scale quadratically with the work, not linearly.

That structural problem compounds with operational fragility. When an agent crashes mid-task, work is lost. When two agents need to collaborate, they hand each other walls of text. Every new external tool — GitHub, Slack, Linear, internal systems — needs its own bespoke authentication, error handling, retry logic, audit trail, cost tracking. Teams end up building the same plumbing five times, badly, before they ever get to differentiation.

The honest measurement: in the headline benchmark, a five-source research task running without Plinth burns through 23,704 to 26,292 tokens depending on topic. The same task running on Plinth uses 6,795 to 7,601. That is a measured 71-72% reduction across three independent topics, run end-to-end, fully reproducible from a clean clone.

> **Agents today are wrappers around interfaces built for humans. That's quadratically expensive. Plinth makes the agent the first-class user.**

---

## 3. Status Quo — Where We Are Today

| | |
|--|--|
| **Current milestone** | v0.6 — Distribution & Polish (May 7, 2026) |
| **Tests passing** | 1,621 across 11 test suites |
| **Codebase** | ~110,000 lines of source and documentation |
| **Deployable units** | 9 (4 platform services, 3 production tool integrations, 1 mock tool server, 1 worker pool, 1 benchmark suite) |
| **End-to-end demos** | 5, all running from a clean clone |
| **Repository** | Private GitHub at `github.com/nico-schindlbeck-jpg/plinth` |
| **License** | Apache 2.0 |
| **Headline benchmark** | 71% token reduction, measured across three independent topics |
| **Backwards compatibility** | Every demo from v0.1 onward still runs unchanged |

The platform has shipped six versions in three days of focused build, each one additive. v0.6 is feature-complete for an early-customer launch.

---

## 4. Core Capabilities — What It Actually Does

### 4.1 Persistent Agent Workspace

> *In plain English: every agent gets its own desk. Things stay on the desk between conversations.*

Each agent gets a private, structured workspace it can write to and read from. It stores facts under named keys, drops longer documents in as files, takes snapshots of its progress, and branches off variants when it wants to try alternatives. Crucially, the workspace survives crashes, restarts, and hand-offs — so an agent that gets interrupted at 3pm can be resumed by another agent at 4pm without losing what it learned. **Business outcome: agents stop paying to remember what they already knew.**

### 4.2 Unified Tool Gateway

> *In plain English: one secure switchboard for every external service the agent talks to.*

Instead of integrating GitHub, Slack, Linear, and internal tools five separate ways, agents make one kind of call to one gateway. The gateway handles secure sign-in to those services, caches identical calls so they cost nothing the second time, enforces per-agent rate limits and dollar ceilings, and writes a complete audit record of every invocation. **Business outcome: integration work stops being bespoke and becomes a checklist.**

### 4.3 Multi-Agent Coordination

> *In plain English: agents pass each other notes through a structured queue, not a chat transcript.*

When one agent finishes its part of a task, it drops a structured message into a typed channel. The next agent picks up just that message — not the entire conversation history that produced it. Channels can validate the messages they receive against a schema; bad messages route to a dead-letter queue for inspection rather than crashing the pipeline. **Business outcome: pipelines compose cleanly, and prompts stop bloating with another agent's deliberations.**

### 4.4 Resilient Workflows

> *In plain English: long-running work survives crashes. Another worker picks up where the last one stopped.*

Multi-step jobs are broken into checkpointed steps. Workers lease a step, execute it, and report back. If a worker dies, its lease expires and another worker takes over from the last good checkpoint — verified end-to-end in the bundled crash-recovery demo. Risky multi-tool sequences can be wrapped in transactions: if any step fails, the system runs the registered undo actions in reverse order. **Business outcome: agent work no longer evaporates when something goes wrong.**

### 4.5 Production Safety

> *In plain English: the things every enterprise will ask about, already built.*

Authentication is real (capability tokens, automatic key rotation, multi-tenant isolation). Rate limits and rolling cost caps prevent runaway spend. Every action is auditable. Schema validation rejects malformed inputs at the boundary. The system can be wired to standard observability backends so each agent action emits a structured event. **Business outcome: when the security review arrives, the answers exist.**

---

## 5. How It Works — The Functional Mechanic

### The mental model

Imagine you've hired a freelance researcher to write you a report. There are two ways to manage them.

**Way A — today's typical agent:** you keep them on a phone call the whole time. Every time they want to remember something, they read it back to you out loud. Every time they pick up a new source, they read it aloud again. By hour three, the call is enormous, every minute costs a fortune, and they've half-forgotten what they decided in hour one.

**Way B — Plinth:** they have a desk. They put sources on the desk. They write notes in a notebook. When they want to refer to source three, they pick it up off the desk — they don't read it aloud again. The desk is still there tomorrow. If they get sick, a colleague can sit at the same desk and continue exactly where they left off.

Plinth is the desk, the notebook, and the filing cabinet for AI agents.

### What happens when an agent runs through Plinth

1. **The agent opens its workspace.** A private space appears, scoped to this agent and tenant, ready to receive structured state.
2. **The agent calls external tools through the gateway.** If the same call was made recently with the same arguments, the cached answer comes back instantly at zero cost. Otherwise the gateway authenticates, checks rate limits and cost caps, calls the real tool, and records the entire interaction.
3. **The agent stores what it learned in the workspace** under named keys, instead of cramming it back into the prompt. Future reasoning steps reference state by key, so the prompt stays small and focused.
4. **If the task involves multiple agents,** they coordinate by dropping structured messages into channels. Each agent reads only the message it needs, not the conversation that produced it.
5. **If the task is long,** it runs as a workflow. Each step is checkpointed. If a worker crashes, another picks up where it stopped — guaranteed by lease-and-heartbeat machinery the platform provides for free.

A non-technical reader walks away with: *the agent reasoned about a small, focused prompt. The substrate handled memory, tools, retries, costs, and coordination invisibly underneath.*

---

## 6. The Roadmap — Where This Is Going

| Version | Theme | Status |
|--------:|-------|:-------|
| v0.1 | Proof of concept — workspace, tool gateway, headline 71% benchmark | Done |
| v0.2 | MVP — multi-agent channels, resumable workflows, dashboard, production guardrails | Done |
| v0.3 | Production-credible — real authentication, multi-tenancy, first live tool integration | Done |
| v0.4 | Scale and operability — production database, observability export, more tool integrations | Done |
| v0.5 | Reliability and depth — schema migrations, durable worker pool, transactions, benchmarks | Done |
| **v0.6** | **Distribution and polish — federated revocation, rollback, lock primitives, workflow visualization** | **Current** |
| v0.7 | Multi-tenant SaaS posture — TypeScript worker harness, per-tenant quotas, admin UI | Next |
| v1.0 | General availability — multi-region, compliance certifications, stable interface guarantees | Future |

> **v1.0 is the moment Plinth becomes the obvious purchase order: a horizontally-scaled, certified, multi-region substrate that any team running production agents would rather rent than rebuild.**

---

## 7. The Bet

Three forces converge in the next eighteen months. First, every serious team running agents in production hits the same wall — the quadratic-cost wall, the resilience wall, the integration wall — and most of them try to build their own substrate before realizing how much non-differentiating plumbing that involves. Second, the standard for tool exposure has stabilized, which means an independent platform can sit cleanly above the protocol layer rather than trying to define one. Third, as model prices fall and agents take on bigger jobs, total token spend per workload climbs — making efficiency gains the difference between a unit economics that work and one that doesn't.

The strategic question is not whether teams will need this layer. They will. The question is who provides it. The three plausible answers: each team builds it themselves (expensive, half-finished, non-differentiating), a hyperscaler ships it tied to their own model (vendor-locked, slow to evolve), or an independent platform wins (model-agnostic, focused, the obvious choice). Plinth is built for the third outcome. The moat compounds with every integration shipped, every benchmark improved, every customer whose workflows live on the platform.

The commercial story at v1.0 is straightforward: a usage-priced, multi-tenant, hosted runtime — with a self-hosted option for regulated buyers. The proof of concept is in the repository today: 1,621 tests passing, five end-to-end demos, a 71% efficiency benchmark anyone can reproduce in five minutes, and six versions shipped without a single breaking change. The platform exists. The question is how fast we get it in front of the buyers who already feel the problem.

> **In two years, every team running production agents will have some form of this layer. We're building the one they should buy.**
