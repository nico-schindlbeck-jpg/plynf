# ADR 0003: MCP as the Tool Protocol

- **Status**: Accepted
- **Date**: 2026-05-05
- **Deciders**: The Plynf Authors

## Context

The gateway (`docs/architecture/03-tool-gateway-design.md`) is the chokepoint where every external tool call passes through. The agents on the caller side can be written in any framework. The tools on the backend side can be written by anyone — vendors, customers, the open-source ecosystem. The gateway needs a **protocol** for talking to backends.

The candidates in the 2025–26 landscape:

- **MCP (Model Context Protocol)** — Anthropic's open standard for tool exposure. Widely adopted as of 2026: most major LLM providers, the OpenAI Agents SDK, every IDE-side agent integration. Defines tool discovery, invocation, streaming, and capability negotiation over JSON-RPC.
- **OpenAPI / REST** — universal but unstructured for our purposes. No standard for "discover and invoke an agent-shaped tool with input/output schemas".
- **gRPC services** — the "right" answer for performance, the "wrong" answer for the agent ecosystem we live in. No agents speak protobuf natively in 2026.
- **A bespoke Plynf protocol** — full control, but every tool author has to learn it, and we'd compete with MCP's existing ecosystem rather than benefit from it.

Strategic context: MCP exists, is open, has Anthropic's backing, and has the agent-side ecosystem already integrated. Tools will be written for MCP regardless of what Plynf picks. Choosing anything else means losing access to that pool.

We also need to be able to **add** semantics MCP doesn't have. Plynf's gateway needs to know: is this tool idempotent? Has the tool author declared a cache TTL? What is the side-effect class (read / write)? What capability scopes does invoking this tool require? MCP's tool schema today does not standardise these.

## Decision

**Adopt MCP as the tool exposure protocol. Layer above it (the gateway is the *agent's* MCP client; backends are MCP servers). Extend MCP with Plynf-specific tool metadata via documented additive fields. Long-term: contribute these extensions back to the MCP spec.**

Concretely:

- The **gateway is an MCP client.** The `transport` field on `ToolRegistration` (CONTRACTS.md) is `"http"` (MCP-over-HTTP) or `"stdio"` (MCP-over-stdio).
- **The Plynf API the agent sees is not MCP.** The agent calls `POST /v1/invoke` against the gateway with our own request shape. The gateway translates to MCP under the hood. This is deliberate: it gives us room to add caching/audit/idempotency without requiring every agent framework to learn Plynf-specific MCP extensions.
- **Plynf-specific tool metadata is added at registration time.** Fields like `idempotent`, `cache_ttl_seconds`, `side_effects`, and (post-v0.1) `required_capabilities` live on `ToolRegistration`. They are declared by whoever registers the tool with Plynf (often the tool author providing config, but operators can override).
- **MCP tools that don't declare these get safe defaults.** `idempotent=False`, `cache_ttl_seconds=None`, `side_effects="write"`. Conservative — better to under-cache than to wrongly cache a side-effecting call.
- **We commit to upstreaming.** Once the field semantics stabilise (post-v0.2), we write a proposal for MCP spec extensions covering caching directives, idempotency hints, and side-effect classes. This makes Plynf additive to the ecosystem rather than a fork.

## Consequences

### Positive

- **Massive existing tool catalog.** Every MCP server out there is, in principle, a tool we can register. The gateway's `transport: "http"` plus the MCP server URL is enough for many backends.
- **The agent-side framework integration is already done.** LangChain/LangGraph/OpenAI Agents/Anthropic SDK all speak MCP. Their existing tool definitions can target our gateway because *we* speak MCP to backends.
- **Familiar mental model for tool authors.** "Write an MCP server" is a known shape. We don't ask anyone to learn a new protocol to ship a tool to Plynf.
- **Observable separation of concerns.** Plynf-specific concerns (caching, audit, capabilities) live in our API. MCP-specific concerns (tool list, invoke, schema) live in MCP. Each evolves independently.
- **Backwards-compatible SDKs.** Our SDK methods like `tools.invoke("web.fetch", {...})` are stable; the wire protocol the gateway uses to reach the backend can change without affecting them.

### Negative / Trade-offs

- **MCP is still evolving.** The spec has had breaking changes through 2024–2025. We pin against a specific spec version (currently MCP 1.0) and update deliberately, not opportunistically. We accept that this means lagging the bleeding edge of MCP for a quarter or two.
- **Two layers of error semantics.** A tool failure can be an MCP error from the backend (mapped to `TOOL_INVOCATION_FAILED`) or a Plynf error (cache miss + backend timeout, capability denied, etc.). The error-model in CONTRACTS.md handles this, but it's more complexity than a "single-protocol" world would have.
- **Plynf-specific metadata is not portable.** A tool registered with Plynf's `cache_ttl_seconds=300` doesn't carry that metadata into a non-Plynf MCP-using framework. Until the upstream extensions land, this is a Plynf-only enrichment. Acceptable: the alternative is to be MCP-only and forfeit the differentiation.
- **MCP's transport choices are partly stdio.** Stdio works fine for local tools but is awkward for a multi-replica gateway (you'd need to manage subprocess lifecycles per replica). We accept stdio for v0.1 because the dev loop demands it; v1.0 production deployments will recommend HTTP transport.
- **Spec extension proposals can be rejected.** Upstream may not adopt our extensions verbatim, or at all. The fallback is to ship them as documented Plynf conventions; the cost is interop friction we'd rather avoid.

## Alternatives Considered

### Build a new tool protocol (Plynf Tool Protocol)

We could specify our own JSON-RPC variant tuned for Plynf's audit/caching/capability story from day one. Reasons against:

- **Ecosystem cost.** Tool authors would have to learn it. The most likely outcome is they don't, and we end up with a thin tool catalog.
- **MCP is already generic enough.** MCP's tool schema covers what we need at the *protocol* layer. Our additions are *metadata*, which we can graft on without reinventing transport.
- **Strategic fit.** A new protocol from a small startup competes with an Anthropic-stewarded standard. Hard to win.

Rejected.

### OpenAPI/REST as the contract

Tools are OpenAPI specs; the gateway uses the spec to invoke them. Reasons this is tempting:

- Tool authors already write OpenAPI for their APIs.
- Schema validation, code-gen tooling everywhere.

Reasons we rejected:

- OpenAPI describes APIs designed for human-driven calls, not agent-shaped tools. The semantics we need (single-invocation tool with structured input/output, capability negotiation, side-effect class) are absent.
- Bridging OpenAPI to "an agent's tool" requires picking which routes are tools, naming them, and synthesizing the agent-side description. That's the work an MCP server already does.
- Tools are a subset of "things an agent might call"; our gateway is the bridge for that subset specifically. Keeping the protocol scoped to "tools" rather than "any HTTP service" is a design clarity win.

We do still **support** wrapping arbitrary REST APIs by registering an MCP-shim server. That's standard MCP practice; not Plynf-specific.

### gRPC

Considered for performance reasons. Rejected:

- No agent ecosystem speaks gRPC. Adoption cost is high.
- The performance gains over HTTP/JSON would matter at much higher throughput than our v0.1 / v1.0 targets. Premature.
- We keep a `specs/proto/` directory aspirationally for the day this matters internally between Plynf services. Tool-facing, no.

### Be agnostic — support all of {MCP, OpenAPI, gRPC} via pluggable backends

Considered. Rejected as overspecification: we'd carry three protocols' worth of complexity for the case where MCP doesn't suit. The cost of "if you want a non-MCP backend, write an MCP shim" is low for the tool author and high for us if we promise three first-class paths. If a customer needs a non-MCP path enough, we'll add a specific transport (e.g. an OpenAPI-direct mode) under the same `transport` field.

## Notes / Links

- Tool registration shape: [`CONTRACTS.md`](../../CONTRACTS.md) §`ToolRegistration`
- Caching, idempotency, side-effect classes (Plynf additions): [`docs/architecture/03-tool-gateway-design.md`](../architecture/03-tool-gateway-design.md) §3, §6
- Capability scopes (post-v0.1 addition): [`docs/architecture/06-identity-capabilities.md`](../architecture/06-identity-capabilities.md)
- MCP spec: external — the version we pin against is documented in `services/gateway/pyproject.toml` (planned) under `mcp-client>=…`.
- Cost-reporting convention from tools: see Open Questions in [`docs/architecture/05-observability.md`](../architecture/05-observability.md) §9 — candidate for upstream MCP extension.
