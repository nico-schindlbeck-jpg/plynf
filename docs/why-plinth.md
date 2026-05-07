# Why Plinth?

> The strategic context. Read this if you want to understand the bet, not just the code.

## The current paradigm is wrong

AI agents in 2026 are mostly wrappers around interfaces designed for humans:

- **Browser automation** (Computer Use, Browserbase, Skyvern) — pixels in, clicks out. Slow, expensive, brittle.
- **API call helpers** — better, but APIs were designed for human-driven CLIs and dashboards, not for agent reasoning patterns.
- **Chat-as-state** — agents reconstruct their world from conversation history every step. Tokens balloon, reasoning gets lossy.

The cost of "agent-as-human" is enormous:
- 30–60% of tokens spent re-reading state
- Workflows that crash mid-flight have no good resume mechanism
- Hand-offs between agents are lossy text passes
- Each new tool integration is bespoke OAuth + auth + audit + error handling
- "Did the agent actually do that?" is hard to answer

## The right paradigm: agent-as-first-class-tenant

If we stop pretending the agent is a person clicking buttons and treat it as a first-class user of dedicated infrastructure, the picture changes completely:

- **State lives in a structured workspace**, not in chat. Agents *reference* state by key, they don't re-read it.
- **Tools live behind a gateway**, not 10 separate API integrations. One auth boundary, unified caching, audit, dry-run.
- **Workflows are durable**. A snapshot before a risky step lets the agent (or another agent) resume on failure.
- **Multi-agent coordination is structured**. Typed channels, not text hand-offs.
- **Identity is agent-scoped**. Capability tokens encode exactly what the agent may do.

This isn't just nicer-to-build. It's **measurably cheaper**. Our research-agent demo shows 60%+ token reduction on a realistic 5-source research task — without changing the underlying model.

## Why now?

Three forces converge in 2025–26:

1. **MCP is real**. A standard for tool exposure means we can build *above* the protocol layer.
2. **Real agents in production**. Devin, Sierra, internal agent platforms — there's a new class of buyer who feels the pain daily.
3. **The token economics matter**. As models get cheaper *per token*, agentic workloads use more tokens. Reduction matters for unit economics.

## What Plinth is (and isn't)

Plinth is **infrastructure for production agents**. It is:
- A persistent workspace (KV + files + snapshots + branches)
- A semantic tool gateway (caching + audit + auth + dry-run)
- A coordination layer (multi-agent primitives) — *coming in v0.2*
- An observability plane — *coming in v0.2*
- An identity layer (capability tokens) — *coming in v0.2*

Plinth is **not**:
- An agent framework (like LangChain) — frameworks are in-process, we're a hosted runtime
- A vertical agent product (like Devin) — we're substrate, not endpoint
- An LLM gateway (like OpenRouter) — we sit *above* model layer, agnostic to model
- A browser-for-agents (like Browserbase) — we replace the pixel paradigm, not host it

## The mental models

Build the **AWS for agents**: horizontal infrastructure, multi-tenant, usage-priced.
Be the **Stripe for tools**: one integration, every tool works, audit included.
Sound like **Vercel for agent-runtimes**: developer love, opinionated defaults, deep observability.

## The bet

In 2–3 years, every team running production agents will have *some* form of this layer. Either:
- They build it themselves (expensive, half-baked)
- A hyperscaler ships it (vendor-locked to their model)
- An independent platform wins (Plinth)

We're building for the third option.

## Read more

- [`README.md`](../README.md) — quickstart
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) — how the pieces fit
- [`docs/architecture/`](./architecture/) — component-level designs
- [`docs/adr/`](./adr/) — every important "why" decision
