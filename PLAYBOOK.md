<div align="center">

# Plynf Playbook

**The founder's manual for showing Plynf and selling Plynf.**

`v1.0 GA` · `2066 tests` · `5 demos` · `May 8, 2026` · `API v1 stable`

</div>

---

This document gives you (a) the exact mechanic to demo Plynf live, click by click, in 30 minutes, and (b) the storyline to position Plynf as the agent-native substrate the next decade of AI infrastructure will be built on.

Opinionated on purpose. When two phrasings exist, only the stronger one is here. Where something is rough, it is named as such — quietly.

> Read Part 1 the night before a stakeholder call. Read Part 2 weekly until you can recite it cold.

---

# PART 1 — Demo Execution Playbook

A 30-minute external-stakeholder showcase has one job: turn the time a stranger gave you into the next conversation. Everything below is engineered for that.

---

## 1.1 — Pre-Flight Checklist (the night before)

Do this the **evening before** the call, not 10 minutes before. The five-minute version of this checklist has cost founders five-figure deals.

### Repository

- [ ] On `main`, clean working tree (`git status` shows nothing)
- [ ] HEAD is the v0.6 release commit; README shows v0.6 / 1621 badge
- [ ] `git pull` ran successfully (no surprise upstream changes)

### Environment

- [ ] Python 3.11+ (`python3.11 --version`)
- [ ] `.venv/` exists; `make install` runs cleanly if not (~2-3 minutes)
- [ ] `make test` passes — every suite green
- [ ] No leftover services: `make stop`; `lsof -iTCP:7421-7428 -sTCP:LISTEN` is empty

### Live boot

- [ ] `make serve` starts all 8 services without error
- [ ] `bash scripts/healthcheck.sh` returns 200 on every endpoint
- [ ] Dashboard at `http://localhost:7424/` loads — workspace list, audit feed, cost rollup, and the new `/workflows` SVG graph (v0.6)
- [ ] `tail -n 5 /tmp/plinth-logs/*.log` shows no warnings

### Five demos run end-to-end

- [ ] `make demo` — 71% reduction table on `renewable energy`
- [ ] `make demo-handoff` — three-agent pipeline, ~8.7k tokens
- [ ] `make demo-resume` — crashes at `outline`, run 2 resumes, exit 0
- [ ] `make demo-triage` — simulation mode produces 10-issue triage report
- [ ] Demo 05 — `plinth-workflow-worker --handlers-module handlers --concurrency 2` in terminal 1; `python examples/05-durable-workflow/start_workflow.py --topic "renewable energy"` in terminal 2; workflow completes

### Warm-up run (within the last hour)

- [ ] Run `make demo` once on the topic you'll use live. Caches populate; first-run surprises happen on your terminal, not in front of the stakeholder.
- [ ] Click into a workspace in the dashboard manually — KV, files, snapshots, channels. Confirm everything renders.

### The room

- [ ] Terminal font 16-18pt for screen-share
- [ ] Browser zoomed to 110-125%
- [ ] All other tabs closed. No Slack, no email, no notification overlays
- [ ] Terminal 1 pinned to project root; terminal 2 in `examples/05-durable-workflow` for the worker scenario
- [ ] Backup browser tab open to the GitHub repo (proof-of-existence if `make serve` melts down)

### Backup story

If a service dies mid-call, the recovery is one sentence and one shell command — never an apology and never a debug session.

> *"One service hiccupped — give me 5 seconds while it restarts."*
>
> Then: `make stop && make serve` (or, surgically: `make serve-<svc>`). 8 services, ~3 seconds to come up. Continue from where you were.

If everything is on fire, fall back to the **GitHub repo + CHANGELOG**. The artifact stands on its own.

---

## 1.2 — The 30-Minute Live Showcase Script

This is the script. You can paraphrase, but the time budget and the **order** are non-negotiable: hook → proof → differentiator → real-world → vision → ask.

### Scene 1 — Minutes 0-3 — The Opening

**The hook.** No small talk past minute 1.

**What you say:**
> *"Today's agents are wrappers around interfaces designed for humans — clicking buttons, reading screens, re-loading the same conversation history every step. That's slow, expensive, and it's why agent products die in production. Plynf flips it. The agent becomes the first-class user of dedicated infrastructure. Let me show you the headline number."*

**What you run:**

```bash
make demo
```

**What the audience sees:** The boxed token-comparison table prints on screen.

```
  Baseline (no Plynf):        23,704 tokens   |   $0.0810
  With Plynf:                  6,795 tokens   |   $0.0345
  Reduction:                     71.3 %        |   $0.0464 saved
```

**What you say while it prints:**
> *"Same task. Same five sources. Same prompts. The only difference is the substrate underneath the agent. 71% fewer tokens. Reproducible from a clean clone — there's no magic, it's structural."*

**What can go wrong:** The demo prints `Services not reachable...falling back to in-process`. Recovery: *"Same numbers either way — the demo bundles fixtures so it works offline. Real services are running too; let me show you those next."*

---

### Scene 2 — Minutes 3-8 — The Dashboard Tour

**Pivot from the number to the surface.**

**What you say:**
> *"That number isn't a benchmark — it's a side-effect. What actually exists is a runtime. Let me walk you through it."*

**What you click:** Open `http://localhost:7424/` in the pre-loaded browser.

**Walk in this order, ~30 seconds each:**

1. **Workspace list** — *"Each agent gets a versioned workspace. KV, files, snapshots, branches. This is the agent's filesystem and database, not its chat history."*
2. **Audit feed** — *"Every tool call recorded. What was called, by which agent, in which tenant, what it cost, what came back. Forensics for free."*
3. **Cost rollup** — *"Per-agent, per-tenant cost ceilings. 1-hour and 24-hour rolling windows. The agent literally cannot blow the budget."*
4. **OTLP status panel** — *"Every audit event also goes out as an OpenTelemetry log. Datadog, Tempo, Honeycomb — pick your poison."*
5. **Workflow graph view** *(v0.6, click `/workflows`)* — *"Visual map of in-flight, completed, and failed steps. Click a node, see the snapshot. This shipped two days ago."*

**What can go wrong:** Dashboard shows "0 workspaces." Recovery: *"Need a warm run — one second"* → run `make demo` in the other terminal → refresh.

---

### Scene 3 — Minutes 8-15 — Watch It Break, Then Heal

**This is the differentiator. Slow down. Don't rush.**

**What you say:**
> *"Most agent demos are happy-path only. The reason agents don't survive in production is failure handling. Watch what happens when an agent crashes mid-task."*

**What you run:**

```bash
make demo-resume
```

**What the audience sees:** The 6-step pipeline starts. After steps 1-3 complete, the agent crashes at `outline` with exit code 99 — visible in the output. Then run 2 starts, calls `wf.resume_info()`, and continues from `outline` without redoing `discover`, `fetch`, or `extract`.

**What you say at the crash:**
> *"That was a real `sys.exit(99)`. No recovery code in the agent. The next process is fresh — no in-memory state. Watch."*

**What you say at the resume:**
> *"It read the workflow manifest from durable storage, found the next pending step, restored the workspace from snapshot, and continued. The agent code didn't change. Resume is automatic."*

> The line that lands: *"In a 6-step pipeline, a crash on step 4 with no resume support means redoing 3 steps' worth of tokens. Plynf makes long-running agents viable in production. That's the whole game."*

**Optional power move (if time allows, ~3 extra minutes):** Switch to demo 05 and **kill a real worker** mid-flight in front of them. `Ctrl+C` in terminal 1, then `plinth-workflow-worker --handlers-module handlers --concurrency 2` again. The lease reaper expires the dead lease, the new worker picks up. This is more dramatic but takes longer because the default reaper interval is ~60 seconds.

**What can go wrong:** The crash exit code prints as `0` (rare — means the crash mock didn't trigger). Recovery: *"Let me re-run with `--crash-at write` instead"* → `python examples/03-resumable-workflow/crash_resume.py --topic "renewable energy" --crash-at write`.

---

### Scene 4 — Minutes 15-22 — The Real-World Demo

**Show that this isn't a research toy.**

**What you say:**
> *"Now let me show what this looks like with a real-world tool integration. GitHub, OAuth, multi-tenant — production-shaped."*

**What you run:**

```bash
make demo-triage
```

**What the audience sees:** A triage agent classifies 10 issues into bug / feature / question / spam buckets and writes a markdown triage report. Then briefly switch back to the dashboard to show the audit log entries that just appeared from the demo.

**What you say:**
> *"Eight services running coordinated right now: workspace, gateway, identity, dashboard, mock-MCP, plus three real OAuth-backed MCP servers — GitHub, Slack, Linear. The agent itself never touches a token. The gateway holds OAuth state, encrypted at rest with AES-256-GCM, and forwards bearer tokens at the edge."*

> The line that lands: *"Most agent frameworks are one process and a Python loop. Plynf is **nine deployable units** sharing JWT capability tokens, with auto-rotating RS256 keys and per-tenant cost caps. This is what real production posture looks like."*

**What can go wrong:** Triage demo says `(example 04 not built yet)`. Recovery: *"Let me show the dashboard view of the same data"* — the workspace and audit log from the prior demos still tells the story.

---

### Scene 5 — Minutes 22-28 — What Does This Scale To?

**Pull back. Show the artifact, not the demo.**

**What you click:** GitHub repo in the second browser tab. Three things:

1. **`Tests` badge** — *"1621 tests. 1503 Python plus 118 TypeScript. Every PR runs them all."*
2. **`CHANGELOG.md`** — *"Six versions in two days. v0.1 was workspace + gateway. v0.6, two days later, has federated revocation, Postgres advisory locks, migration rollback, generic resource locks, schema migration helpers, and a workflow graph. Every demo from v0.1 still produces unchanged output. That's the engineering culture."*
3. **Architecture diagram in the README** — *"Workspace, gateway, identity, dashboard, three OAuth providers, a worker pool. Apache 2.0. Postgres-ready. OTLP-instrumented."*

**What you say:**
> *"What you just saw is v0.6. v0.7 is multi-tenant SaaS posture: TS worker harness, per-tenant quotas, schema-evolution wizard. v1.0 is GA with compliance certifications."*

> The line that lands: *"This isn't a thesis I'm hoping someone funds. It's a runnable artifact. You can clone it tonight and run every demo in fifteen minutes."*

---

### Scene 6 — Minutes 28-30 — Close + Ask

**Be direct about what you want from them.** Founders forget the ask. Don't.

**What you say (pick one based on stakeholder):**

- *"What I want from this conversation is a follow-up next week with two of your portfolio CTOs. I want their pushback on the architecture before I go raise."*
- *"What I want is a 30-day design partnership: one of your real workflows, behind your firewall, on Plynf. No fee. I learn what breaks; you learn whether this saves you the 70%."*
- *"What I want is honest 5-minute feedback now, then a yes/no on whether this is in your fund's scope."*

Then: stop talking. Wait.

---

## 1.3 — Common Failure Modes & Recovery

These are the realistic ones. Memorize the recovery line.

| Symptom | Likely cause | Recovery line in 10 seconds |
|---|---|---|
| `make serve` says `Address already in use` | Leftover services from a prior session | `make stop && make serve` |
| Demo prints `0% reduction` or runs in 0ms with weird numbers | Mock-MCP not running; in-process fixtures masking | `make healthcheck`, then `make serve-mock` |
| Dashboard returns 502 / blank panels | Upstream workspace or gateway down | `tail -n 30 /tmp/plinth-logs/dashboard.log`, then `make serve-workspace` (or whichever is dead) |
| Demo says `Services not reachable: workspace, gateway, mock_mcp. Falling back...` | One or more services not started | Acknowledge briefly: *"Numbers are identical — the demo bundles fixtures."* Then `make serve` and re-run |
| `make demo-resume` exits 0 with no crash printed | `--crash-at` step name typo or stale workspace | `python examples/03-resumable-workflow/crash_resume.py --topic "renewable energy" --crash-at write` (use a different step) |
| Triage demo prints `(example 04 not built yet)` | `examples/04-github-issue-triage` not pip-installed | `.venv/bin/pip install -e examples/04-github-issue-triage`, re-run |
| Worker (demo 05) doesn't pick up steps | Worker not registered against the right module | Cd into `examples/05-durable-workflow` first, then run `plinth-workflow-worker --handlers-module handlers --concurrency 2` |
| `make test` shows 15 skipped | Postgres tests gated on `PLINTH_TEST_POSTGRES_URL` | Expected — explain: *"15 Postgres tests skipped because we don't have Postgres running on this laptop. Wired and tested in CI."* |
| Browser shows old data on dashboard | Cached SPA bundle | Hard refresh (`Cmd+Shift+R`) — the dashboard polls every 5s but the bundle itself caches |
| Live mode demo asks for `ANTHROPIC_API_KEY` | You ran without `--mode simulation` and the key isn't set | Re-run with simulation; the structural number is the same. Tell them: *"Live mode produces a comparable or slightly larger reduction; simulation is deterministic and demo-safe."* |

---

## 1.4 — Stakeholder-Specific Variations

Same playbook, different emphasis. Don't try to do all four versions in one call — pick the one that matches the seat across from you.

### For an early-stage VC

Lead with **71%**, **1621 tests**, **v0.1→v0.6 in two days**. They want pattern-matching: working artifact + fast velocity + crisp metric. Save the dashboard for last as proof of polish. Skip the architecture diagram unless asked. End with a design-partner ask, not a fundraise ask — let them bring up money.

### For a potential design partner / customer

Lead with **their** workflow. Before the call, swap the demo topic from `renewable energy` to something native to their domain. Show **the cost ceiling and audit log first** — the controls a buyer needs to greenlight a pilot. Skip the velocity story; nobody buying infrastructure cares that you wrote it fast. Show resumability second — it's the risk-reduction story for them. End with: *"30 days, no fee, your firewall, one of your real workflows."*

### For a CTO or engineer co-founder candidate

Skip the marketing veneer. Open with `make test` showing 1621 green. Then `make demo-resume` with a verbal walk through the lease + heartbeat architecture. Then `tree services/` and walk the architecture. Show [`CONTRACTS.md`](./CONTRACTS.md) and [`docs/adr/`](./docs/adr/). End with: *"What would you change first?"* Their answer tells you whether they're the right co-founder.

### For an existing-company executive (acquisition / partnership)

Emphasize the 9-service architecture, multi-tenancy, OAuth providers (GitHub / Slack / Linear), audit trail, OTLP — *integration breadth* and *compliance-readiness*. Skip the indie-velocity narrative; for them, "two days" reads as "not de-risked." Reframe v0.6 as *"feature-complete substrate, pre-GA, currently engaged with N design partners"* (adjust N to honest reality). End with: *"What would a partnership look like — reseller, integration, or build-vs-buy?"*

---

# PART 2 — Positioning & USP

A wrong pitch kills a right product. This part is what you say across the next 100 conversations. Memorize the core, adapt the surface.

---

## 2.1 — The Core Thesis

> **The next infrastructure layer is the agent-native substrate. Plynf is the substrate.**

Every cloud era was unlocked by a layer designed for the new first-class user: AWS for servers, Stripe for payments, Vercel for frontends. The current era's first-class user is the **AI agent** — and today it's stuck running on infrastructure built for humans (chat history as memory, browsers as tools, retries as recovery). Every production agent rebuilds the same missing pieces — workspace, tool gateway, channels, durable workflows, identity — badly, in-process, non-portably. **Plynf is those pieces, designed-in from day one, with a measured 71% token reduction on a real workload as proof the primitives are right.**

That paragraph is your North Star. Everything below is its ammunition.

---

## 2.2 — The Three-Layer Storyline

Three frames. Each answers a different prospect's question. **Lead with the one that matches their pain.**

### Frame A — The Cost Frame

**Answers:** *"Why does our agent product lose money on every interaction?"*

**Talking points:**
- Agent context grows **quadratically** when chat history is the only memory. By step 10, you pay ~10× per token vs. step 1.
- Anthropic Sonnet is $3 per million input tokens. A naive agent burns 20-50k tokens for a 5-source research task; a well-engineered one does it in 6-7k. At scale, the gap is gross-margin vs. money-pit.
- Plynf structurally kills the quadratic by holding state in the workspace, not the prompt. Each reasoning step gets a focused prompt referencing keys, not history.

**Proof in the repo:**
- 71.3% / 72.0% / 71.1% on three topics, measured exact via `tiktoken`.
- The `--per-step` breakdown shows the synthesis prompt drops from ~6,000 tokens to ~1,200.
- Gateway-level caching: identical fetches across runs are free. In multi-agent / multi-run scenarios this compounds.

**Don't say:** "We make agents cheaper." **Do say:** *"We make the unit economics of agentic AI work at scale, by killing the quadratic."*

---

### Frame B — The Reliability Frame

**Answers:** *"Why does our agent product fall over the moment we try to ship it?"*

**Talking points:**
- Long-running agents will crash. Networks blip, LLM APIs time out, processes get OOM-killed. Without durable state, every crash means starting over.
- Plynf ships durable execution with **lease + heartbeat** worker semantics (v0.5). Kill a worker mid-step, the lease reaper reverts the step to pending, another worker picks it up. No bespoke recovery code in the agent.
- Workflow transactions support **Saga-style compensating actions** (v0.5). Typed channels with **dead-letter queues** catch malformed messages.
- Migration framework, advisory locks, federated revocation, generic resource locks (v0.6) — production-shaped concerns designed-in, not retrofitted.

**Proof in the repo:**
- `make demo-resume`: 6-step pipeline, `sys.exit(99)` mid-step, run 2 picks up — saves ~32% of the work.
- Demo 05: real worker, real lease semantics. Killable in a live demo.
- 1621 tests including 33 lease tests, 40 transaction tests, 28 channel-schema tests.

**Don't say:** "We solved reliability." **Do say:** *"Your agents survive crashes. The substrate handles it; your agent code stays simple."*

---

### Frame C — The Composability Frame

**Answers:** *"How do we go from one agent to a fleet of coordinated agents without it becoming a tarpit?"*

**Talking points:**
- Multi-agent systems fail when agents share LLM context — token bills explode, debugging becomes impossible.
- Plynf provides **channels**: durable, monotonically-sequenced, optionally-typed queues. Agents communicate via small typed messages, not concatenated prompts.
- Each agent stays small, single-purpose, and **horizontally composable** — add a fact-checker between Writer and Reviewer without touching either.
- Plynf is **the substrate, not another framework**. We don't tell you how to write the reasoning loop. We give you the OS the agent runs on.

**Proof in the repo:**
- `make demo-handoff`: Researcher → Writer → Reviewer in **8,737 tokens total** for a 547-word reviewed report.
- Each agent's prompt is bounded by handoff payload size, not corpus size.
- Three durable channels, three snapshot checkpoints. Replay from any of them.

**Don't say:** "We're a multi-agent framework." **Do say:** *"We're the platform layer that makes multi-agent systems an organizational pattern, not a debugging exercise."*

---

## 2.3 — The Unfair Advantage

Why is Plynf structurally hard to copy? Pick the four sharpest defensibilities. Don't pad.

**1. Agent-native primitives, designed-in.** Workspace + tool gateway + channels + workflows + identity, conceived together, with the same JWT carrying tenant + scope + cost-cap claims across all of them. Existing tools retrofit one piece (Temporal has durable execution; LangChain has agent loops; Auth0 has identity) and assume the rest exists. The integration work is the moat — and it's done.

**2. Benchmark integrity.** The 71% number is reproducible from a clean clone in 15 minutes. Token counts are exact (`tiktoken cl100k_base`), three topics, deterministic simulation mode for regression-safe CI. Most "we cut tokens by X%" claims in the AI space are anecdotes; this one is a runnable artifact in the repo.

**3. Velocity proof + backwards-compat discipline.** Six versions in two days (May 5-7, 2026), 1621 tests, ~110,000 lines of source + docs. **Every demo from v0.1 still produces unchanged output** at v0.6 — verified post-merge each release. That last clause is what tells a sharp engineer this isn't a hack; it's an operating culture.

**4. Production-pipeline coverage, not "the demo works."** Auth, multi-tenancy, OAuth (GitHub + Slack + Linear), audit log, atomic transactions with rollback, OTLP, RS256 with auto-rotation, Postgres backend, schema migrations, advisory locks, dead-letter queues, load-shedding middleware — all in by v0.6. A copycat starting today rebuilds these one at a time, with the integration friction we've already paid.

> The defensibility isn't any one feature. It's the **shape** of the integration — the JWT that carries tenant + scope + cost cap, the audit event that's also an OTLP log, the snapshot that's also a workflow checkpoint. That shape is the moat.

---

## 2.4 — The Competitive Map

| Player | What they are | What they don't do (that we do) |
|---|---|---|
| **LangChain / LlamaIndex / CrewAI** | In-process Python agent frameworks | Persistent runtime; multi-tenancy; OAuth at the edge; durable lease + heartbeat execution; cross-process channels |
| **Sierra / Cognition (Devin) / Adept** | Vertical end-user agent products | Horizontal substrate. They are the customer; we are the layer beneath them |
| **AWS Bedrock / OpenAI Assistants / Vertex Agents** | Vendor-locked agent runtimes | Model-agnostic; multi-tenant by design; OSS Apache 2.0 |
| **Temporal / Inngest / Restate** | General durable execution engines | Agent-specific primitives: workspace state, tool gateway with caching + cost caps, channels, OAuth flows |
| **MCP itself (Anthropic spec)** | A protocol for tool servers | The runtime around it: caching, audit, rate limits, OAuth, multi-tenancy, OTLP. We're built **on top of MCP** |
| **Auth0 / Okta + custom glue** | Identity, generic | JWT scope grammar designed for agent capabilities; per-token rate-limit + cost-cap claims; tenant-scoped from day one |

> If a single existing product gave you all of this, we wouldn't exist. The fact that buyers stitch four of these together to limp into production is the wedge.

---

## 2.5 — The Pitch Variations

All three share a **core** — sentences that never change. The rest adapts. Memorize the core first; learn one expansion at a time.

### The 30-second pitch (elevator)

> *"Plynf is the substrate where production AI agents actually work. Today's agents run on infrastructure designed for humans — chat history as memory, browsers as tools — and that's why they're slow, expensive, and brittle in production. We give the agent its own workspace, a unified tool gateway, durable workflows, and identity — measured 71% token reduction on a real workload, reproducible from a clean clone. Six versions, 1621 tests, in two days. Looking for design partners now."*

### The 3-minute pitch (call opener)

> *"AI agents are the new infrastructure user. The cloud platform for them doesn't exist yet — every team rebuilds the same five primitives badly, in-process, locked to one model provider. Plynf is those primitives, designed-in from day one.*
>
> *Five things every agent that runs on Plynf gets: a versioned persistent workspace, a unified tool gateway with caching and OAuth, durable workflows with lease-and-heartbeat recovery, durable typed channels for multi-agent handoffs, and a JWT identity layer with per-tenant cost caps. Nine deployable units, 8 services, 3 real OAuth providers, fully OTLP-instrumented.*
>
> *Headline result: a 5-source research task uses 71% fewer tokens with Plynf than without. Measured exact, three topics, reproducible in 15 minutes from a clean clone. We have five end-to-end demos including a multi-agent pipeline at 8,700 tokens and a workflow that crashes mid-step and resumes from snapshot.*
>
> *We're at v0.6 — production-credible for design partners, pre-GA. Apache 2.0. Velocity proof: six versions in two days, 1621 tests passing, every demo from v0.1 still produces unchanged output. We're talking to design partners and seed investors right now. What I'd love is 15 more minutes to show you the demo live."*

### The 10-minute pitch (deck flip-through)

This is the 3-minute pitch, slowed down, with a slide per layer:

1. **The problem.** *(45 sec)* Quadratic context growth. The "phone-call freelancer" analogy from `OVERVIEW.md`.
2. **The thesis.** *(45 sec)* Agent-native substrate. The cloud-era pattern.
3. **The five primitives.** *(2 min)* One sentence per primitive. Workspace, gateway, channels, workflows, identity.
4. **The headline number.** *(1 min)* 71% reduction, three topics, exact tiktoken counts. Show the table.
5. **The differentiator: reliability.** *(2 min)* Crash + resume demo, lease + heartbeat, transactions with Saga rollback.
6. **The architecture.** *(1 min)* 9 services, JWT + multi-tenant, OAuth providers, OTLP, Postgres.
7. **Velocity + backwards compat.** *(1 min)* v0.1→v0.6 in two days, 1621 tests, every demo unchanged.
8. **The roadmap.** *(45 sec)* v0.7 SaaS posture, v1.0 GA. What design-partner work informs.
9. **The ask.** *(45 sec)* What you want from this audience.

The **core sentences** that appear in every variation:

- *"Plynf is the substrate where production AI agents actually work."*
- *"Measured 71% token reduction on a real workload, reproducible from a clean clone."*
- *"Six versions, 1621 tests, in two days."*

Three sentences. Elevator, call, deck. That's the spine.

---

## 2.6 — Anti-Pitch — What NOT to Say

Founders kill deals by overclaiming. Brittleness on the left, durable replacement on the right.

| Don't say | Say instead |
|---|---|
| *"We're going to replace OpenAI."* | *"We're model-agnostic infrastructure that makes any LLM work better in production."* |
| *"It's done."* | *"It's production-credible for design partners. v1.0 is when we ship guarantees and certifications."* |
| *"We solved the multi-agent problem."* | *"We have one good answer — typed channels with bounded handoff payloads. The 8.7k-token three-agent demo is in the repo."* |
| *"The 71% is universal."* | *"71-72% on three research-style topics with a 5-source pattern. The structural reason — kill the quadratic — generalizes; the exact percentage doesn't."* |
| *"We're already at scale."* | *"Scale-ready architecture, not scale-yet. Postgres-backed, OTLP-instrumented, load-shedding middleware in place. Real load tests come with design partners."* |
| *"It's enterprise-ready."* | *"Production posture enterprise buyers ask for — multi-tenancy, audit, OAuth, OTLP — with a clear path to SOC 2 / ISO in v1.0."* |
| *"We have no competition."* | *"The closest comparables are Temporal (durable execution, not agent-specific) and LangChain (agent framework, not a substrate). We sit between them; the gap is the wedge."* |

> **The rule:** strength sounds like specificity. Weakness sounds like superlatives. Replace every "best" with a number, every "solved" with a measured boundary.

---

## 2.7 — Marketing Channel Plays

Tactical, not aspirational. Each row is a specific first move.

| Channel | First move | What "winning" looks like |
|---|---|---|
| **Hacker News (Show HN)** | Title: `Show HN: Plynf — agents survive crashes and use 71% fewer tokens (open source)`. Body: repo link, the headline number, one paragraph per primitive, the 15-minute claim. | Top 5 within 6 hours; 100+ comments; 500+ stars in 48h |
| **LinkedIn (founder-led, 3-post series)** | Post 1: pain frame. Post 2: 60-90 sec demo video (`make demo` + `make demo-resume`). Post 3: roadmap + design-partner ask. | 5+ qualified DM replies from CTOs / VPs Eng |
| **AI engineer podcasts** | One-pager to *Latent Space*, *AI Engineer*, *Practical AI*. Lead with the 71%, the 9-service architecture, the velocity story. | One booking inside 6 weeks |
| **Twitter/X** | 90-second screen-capture of crash-resume — exit 99 → resume → completion. No voiceover; let output tell the story. | 50k+ views in 48h; reposts from 3+ AI infra accounts |
| **Indie Hackers / r/MachineLearning** | Architecture deep-dive (not a pitch): "How we built a JWT capability layer that carries tenant + scope + cost-cap across 4 services." | 50+ upvotes; genuine technical discussion |
| **Direct outreach** | 20 hand-picked target CTOs. Personal note with one specific observation about their stack + the design-partner offer. | 4 conversations; 1 design partner |

> Show HN is the highest-leverage move. Schedule it for a Tuesday morning Pacific, repo at HEAD, one team member ready to answer technical questions in the first 90 minutes.

---

## 2.8 — Founder's Cheat Sheet — Numbers & Facts to Memorize

Quote any of these without notes.

- **71.3% / 72.0% / 71.1%** token reduction on `renewable energy` / `ai agents` / `climate policy`.
- Anthropic Sonnet: **$3 / 1M input tokens**, **$15 / 1M output**.
- **1,621 tests** = 1,503 Python + 118 TypeScript.
- **5 end-to-end demos**, all reproducible from a clean clone.
- **9 deployable units**: 4 services (workspace 7421, gateway 7422, dashboard 7424, identity 7425) + 3 OAuth-backed MCP servers (GitHub 7426, Slack 7427, Linear 7428) + 1 mock-MCP (7423) + 1 worker.
- **6 versions in 2 days** (v0.1 May 5 → v0.6 May 7, 2026). Apache 2.0.
- **~110,000 lines** source + docs. **6 ADRs**, **6 architecture sub-docs**.
- **Per-agent rate limits** from 60 RPM down to per-tenant; **cost caps** on 1h + 24h rolling windows.
- **Workflow recovery saves ~32%** of work after a mid-step crash (demo 03).
- **Multi-agent demo:** **8,737 tokens** total for a 3-agent pipeline producing a 547-word reviewed report.
- **JWT capability tokens:** RS256 with auto 30-day rotation, JWKS published, scope-grammar, revocable, multi-tenant. Federated revocation in v0.6.
- **Storage:** SQLite default, Postgres production driver via `PLINTH_STORAGE_DRIVER=postgres`.
- **Observability:** OTLP-out from the gateway to Datadog / Tempo / Honeycomb / OTel Collector.
- **OAuth providers:** GitHub, Slack, Linear. PKCE-correct. AES-256-GCM at-rest encryption.
- **Backwards compat:** Every v0.1 demo still produces unchanged output at v0.6.
- **Known gaps:** TS worker harness deferred to v0.7; schema-versioning UI minimal in v0.6; 15 Postgres tests skipped without `PLINTH_TEST_POSTGRES_URL`.

> **One sentence the founder should be able to say without breath:** *"v0.6, sixteen-twenty-one tests, five demos, nine deployable units, 71% token reduction, two days from v0.1, Apache 2.0 — clone it tonight and run every demo on your laptop in fifteen minutes."*

---

<div align="center">

**End of Playbook.** *Read it before every call. Update CHANGELOG references and test count after every release.*

</div>
