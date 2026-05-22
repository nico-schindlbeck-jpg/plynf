# ADR 0004: Workflow Engine — Temporal for v0.3 Coordination

- **Status**: Proposed
- **Date**: 2026-05-05
- **Deciders**: The Plynf Authors

## Context

[`docs/architecture/04-coordination-primitives.md`](../architecture/04-coordination-primitives.md) specifies durable workflow transactions: a sequence of tool calls and workspace writes that must succeed atomically or compensate back. The engine has to:

- Persist progress at every step (so a crash mid-step doesn't lose us).
- Resume from the cursor after a process restart.
- Handle long-running steps (minutes to hours).
- Run compensations in reverse on failure.
- Surface signals (an agent waits for a channel message or a human approval).
- Be operable by teams that aren't workflow specialists.

Building this from scratch is work we should avoid. There's a small set of mature open-source durable-execution engines:

- **Temporal** — battle-tested at very large scale (Uber, Coinbase, Netflix). Strong multi-language SDKs (Go, Java, Python, TypeScript). Heavyweight runtime: Java/Go services backed by Cassandra or Postgres. Self-host or Temporal Cloud.
- **Restate** — newer, Rust-implemented, simpler operational footprint. SDKs in TS, Java, Python. Designed for the modern serverless era. Less battle-tested at extreme scale; rapidly maturing.
- **Inngest** — managed-first, focuses on developer ergonomics, durable functions. Strong TS story, weaker for Python services. Self-host option exists but is the secondary path.
- **DBOS** — a newer entrant. Postgres-as-state, library-style. Compelling but immature for our needs.
- **Custom (DIY)** — write our own state machine on top of Postgres. Full control, no dependency. Substantial work.

Decision drivers, in priority order:

1. **Durability and replay correctness.** Workflow engines exist because writing this correctly is hard. We weight maturity heavily.
2. **Python SDK quality.** Per ADR 0001, services and the primary SDK are Python. We need to define workflows in Python ergonomically.
3. **Operational footprint.** Adding a heavyweight piece of infrastructure to Plynf's deployment story has a cost; lighter is better.
4. **Multi-language reach.** Some customers may want to register tools or workflows from Go/TS/etc. Multi-language SDKs are a tiebreaker.
5. **Vendor risk / open-source posture.** We should be able to self-host indefinitely.

## Decision

**Use Temporal as the durable execution engine for v0.3 coordination work. Inngest is documented as a fallback for deployments where Temporal's footprint is unjustified.**

Specifically:

- Plynf ships a **`services/workflow` shim** that translates our `WorkflowDef` (see arch doc 04 §4) into Temporal workflows and Plynf steps into Temporal activities.
- The Plynf API (`POST /v1/workflows/{id}/start`, etc.) is unchanged; Temporal sits behind it.
- Self-hosters can choose between:
  - **Temporal cluster** (recommended for production at scale): self-hosted or Temporal Cloud.
  - **Inngest dev server / cloud** (recommended for smaller deployments): a Plynf adapter targeting Inngest's durable-functions API.
- The shim **abstracts the engine**. If a customer wants to swap engines, they re-target the shim, not their workflow definitions.
- Plynf's own *coordination primitives* — channels, locks — do **not** depend on the workflow engine. They live in the workspace/gateway services. Workflows compose with them, but a Plynf deployment can choose to enable workflows or not, independent of the rest.

## Consequences

### Positive

- **Don't reinvent the durable-execution wheel.** Temporal has 7+ years of production hardening. Replay correctness (the core invariant of durable workflows) is something Temporal has shipped, debugged, and battle-tested.
- **Python SDK is mature.** `temporalio` is first-class, async-native, and integrates well with FastAPI. Workflow definitions are decorated Python functions with all the type hints that gives us.
- **Multi-language reach for advanced users.** A customer can write a workflow in Go targeting our Plynf shim, if they want to. Most won't, but the option matters for enterprise.
- **Operability under control.** Temporal's UI, CLI, and observability story are well understood. SREs who've operated Temporal can operate ours.
- **Time travel and replay** are built in, which dovetails with Plynf's own replay story (arch doc 05 §5).

### Negative / Trade-offs

- **Heavyweight runtime.** A Temporal cluster is multiple services (frontend, history, matching, worker), backed by Cassandra or Postgres. Even the dev image is non-trivial. We'd be a serious step up from the v0.1 "everything fits on a laptop" promise.
- **Latency overhead.** Workflow start, activity dispatch, and cursor advancement all hit the Temporal service. For very short workflows, the overhead can dominate. We accept that workflows are for *durable, multi-step, possibly-long-running* work; a single tool call is not a workflow.
- **Operational complexity for self-hosters.** "Run Plynf" goes from "two FastAPI services + SQLite" to "two FastAPI services + Postgres/S3 + Temporal cluster". The Inngest fallback exists to mitigate this for smaller deployments.
- **Lock-in risk to Temporal's programming model.** Temporal workflows have specific constraints (deterministic code, no direct I/O outside activities). Our shim absorbs this for the Plynf `WorkflowDef` shape, but anyone authoring workflows directly against Temporal will need to learn the rules.
- **Dual SDK surfaces.** We end up maintaining the Plynf workflow API + an internal Temporal binding. Conceptually clean, more code to maintain.

## Alternatives Considered

### Restate

Genuinely tempting. Restate is light, has a simpler operational story (a single Rust binary plus a metastore), and its programming model is closer to "just call functions, persistence is ambient" — pleasant for SDK authors. We took a serious look. Reasons we don't pick it for v0.3:

- **Maturity gap.** As of 2026-05, Restate is shipping production users but is younger than Temporal by several years. For a critical-path piece, we want the option of "this is what Coinbase uses".
- **Multi-language SDK breadth.** Restate's SDKs are good but don't cover Go, which is a tiebreaker for enterprise customers.
- **Future option.** If Restate continues to mature, swapping our shim's backend from Temporal to Restate is contemplated and explicitly preserved in the design. The Plynf `WorkflowDef` API is the abstraction that makes this possible.

We will reassess this choice at v0.4 once we have actual workflow workloads.

### Inngest only

Inngest is excellent for the deployment case where you'd rather not run a Temporal cluster. The arguments against making it the primary:

- The strongest SDK is TS; Python is good but a step behind.
- The hosted-first posture is good for some deployments and bad for others (regulated industries with self-host requirements).
- The programming model is "step.run + signals", which is fine but less flexible than Temporal for the multi-agent dance our coordination doc describes.

Hence: **fallback, not primary**.

### Custom on Postgres

Build our own durable executor: a `workflows` table with cursor and step results, a worker process that wakes on signal, periodic timers via `pg_cron` or similar. We sketched it. The case for:

- Zero dependencies beyond Postgres (which we already have per ADR 0002).
- Tight integration with our event log and audit (no impedance mismatch).
- Total control of the programming model.

The case against (which we judged decisive):

- Replay correctness is hard. Years of Temporal bugs and fixes are not easily replicated; we'd ship subtle races in week 1.
- Maintaining a workflow engine is a full-time investment we cannot afford while also building the rest of Plynf.
- It's not where we differentiate. Customers buy Plynf for the agent-native substrate; whether the workflow engine underneath is Temporal or homegrown is invisible to them.

Strong rejection for v0.3. We may reconsider for very-small-scale deployments at v1.0 if we observe customers won't run a Temporal cluster.

### DBOS

A "database as a workflow runtime" library — workflows are Python/TS functions whose state lives in Postgres directly. Conceptually elegant, especially given we already use Postgres. Rejected because:

- Maturity is lower than Restate, let alone Temporal.
- The library shape means the Plynf runtime hosts the workflows in-process, which complicates the multi-replica scaling story.
- We prefer a clear separation between "the workflow service" and "the rest of Plynf" for failure-isolation reasons.

### Step Functions / Workflows-as-a-service from a cloud vendor

Considered (AWS Step Functions, GCP Workflows, Azure Durable Functions). Rejected:

- Vendor lock-in for a critical piece of Plynf's architecture is contrary to our positioning.
- Self-host story collapses.
- We cannot ship a v0.3 that runs *only* on AWS.

## Notes / Links

- Workflow primitives spec: [`docs/architecture/04-coordination-primitives.md`](../architecture/04-coordination-primitives.md)
- Temporal Python SDK: external — `temporalio` on PyPI
- Replay relationship: [`docs/architecture/05-observability.md`](../architecture/05-observability.md) §5
- Compensation semantics: [`docs/architecture/04-coordination-primitives.md`](../architecture/04-coordination-primitives.md) §4
- Reassessment trigger: any of (a) Restate hits 1.0 with strong production references; (b) Temporal's operational footprint becomes a frequent customer objection; (c) DBOS-style ergonomics become a hiring/adoption blocker. Re-open this ADR at that point.
