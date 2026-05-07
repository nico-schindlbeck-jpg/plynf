# ADR 0001: Language and Stack

- **Status**: Accepted
- **Date**: 2026-05-05
- **Deciders**: The Plinth Authors

## Context

Plinth is two FastAPI-shaped services (workspace, gateway), a primary SDK, and (for parity with the Node-side agent ecosystem) a secondary TS SDK. The choice of language stack here is load-bearing: it drives hiring, library availability, ergonomics for AI/ML callers, and the operational posture of the runtime. The decision needs to be made once at v0.1 because changing it post-PoC would invalidate large swaths of work.

The relevant constraints when this decision was made:

- The dominant agent-development ecosystem in 2025–26 is Python-first. LangChain, LangGraph, LlamaIndex, the OpenAI Agents SDK, the Anthropic SDK, every research tool — Python.
- The frontend / Node side has its own (smaller but real) agent-runtime presence: Vercel AI SDK, Mastra, frontend agents embedded in webapps. A TS SDK is a credibility minimum for adoption there.
- Plinth's services are I/O-bound, not CPU-bound. The hot paths are HTTP, JSON, SQL — none of which benefit dramatically from a systems language at this stage.
- We are a small team (PoC). Optimising for development velocity matters more than optimising for runtime efficiency in v0.1.
- Our buyers (production-agent platform teams) have engineers who can read both Python and Go fluently; they expect to *operate* something written in a language they know. Operationally, "Python with uvicorn" is unsurprising and reviewable.

## Decision

**Python 3.11+ for both services and the primary SDK. TypeScript 5.4+ alongside as a secondary SDK with feature parity tracked separately. Rust deferred.**

Specifically:

- `services/workspace` and `services/gateway` are FastAPI apps run on uvicorn (single-process, multi-worker via `--workers`).
- `sdk/python` is the canonical SDK and includes the `@client.agent` ergonomic decorator (see `CONTRACTS.md` §SDK Surface).
- `sdk/typescript` mirrors the Python public surface where ergonomic. v0.1 ships it as a skeleton; full parity is a v0.2 deliverable.
- `mock-mcp-server` is also FastAPI to keep the toolchain uniform.
- No Go, Java, or Rust is introduced in v0.1.

Tooling:
- Package management: `uv` for development, `pip` for distribution. `pyproject.toml` per package.
- Style: `black` (line length 100), `ruff` for lint, `mypy --strict` aspirationally.
- Tests: `pytest` + `pytest-asyncio` + `httpx.AsyncClient` against the FastAPI app.
- See `CONVENTIONS.md` for the full enforcement details.

## Consequences

### Positive

- **Idiomatic for callers.** Agent code is mostly Python; `from plinth import Plinth` lives in the same file as `from anthropic import Anthropic`. No FFI, no code-gen.
- **Library leverage.** `httpx` for HTTP, `pydantic` for models (already in CONTRACTS.md), `tiktoken` for token counting offline, `structlog` for logging — battle-tested and fast.
- **FastAPI's specifics matter.** OpenAPI spec generation is automatic; we publish `specs/openapi/*.yaml` directly from the route definitions. Pydantic models live in one place and serve as both runtime validation and documentation.
- **Velocity.** Small team, fast iteration, no compile step. Deploy a service in seconds.
- **Hireability.** Python backend engineers are the majority population in our target market. Operating teams can read the code without ramp.

### Negative / Trade-offs

- **The GIL.** CPU-bound work (cryptographic chaining of audit events, future merge algorithms over large workspaces) will need careful design. We mitigate by running multiple uvicorn workers (process-level parallelism) and by keeping per-request CPU low. If a hot path turns out to need true parallelism, we will offload it (a Rust extension via `pyo3`, or a sibling service in another language). v0.1 doesn't have such a path.
- **Async runtime quirks.** `asyncio` plus blocking SQLite operations is a known footgun. We use `asyncio.to_thread` for the SQLite calls in v0.1 and plan to move to `asyncpg` for the v1.0 Postgres backend (ADR 0002), which is async-native. Honest about the interim awkwardness.
- **Memory footprint.** A Python uvicorn worker is ~80–150 MB resident. Multiply by workers and replicas; this is fine for our scale but cost-conscious operators will notice.
- **Type system limits.** `mypy --strict` is aspirational. Pydantic v2 helps a lot for I/O boundaries but doesn't replace a real type system. We accept this; the alternative (Go, TS for services) costs more than it saves at v0.1.
- **TS SDK lag.** Maintaining two SDKs at parity is expensive. v0.1 ships TS as a skeleton; we consciously accept that the TS-side use case is "a few weeks behind Python". This is a known cost of a multi-SDK strategy.

## Alternatives Considered

### Go for services, Python and TS for SDKs

Plausible. Go services are operationally great (single binary, low memory, fast). The cost is two repositories' worth of context-switching for the team, and a slower iteration loop on the part of the codebase we'll change most (the API surface) during PoC. We rejected this for v0.1; we reserve the right to rewrite a hot-path service in Go later if profiling demands it.

### Rust for services

Rust would give us memory safety at zero runtime cost, true parallelism, and beautiful single-binary distribution. The price is a 3–5× development-time cost at PoC stage and a much smaller hireable pool in our target buyer population. Our hot paths aren't yet hot enough to need this. **Future option, not now**: if we find ourselves CPU-bound on cache lookup or audit hashing, a Rust component (probably as a `pyo3` extension, not a separate service) is the obvious answer.

### Node/TypeScript for services

Considered briefly. The argument for: unify the language with the TS SDK, attract the frontend-agent ecosystem. The arguments against: most of our target buyers' production code is in Python, our hireable pool is larger in Python, and FastAPI's OpenAPI integration is materially better than any Node equivalent we evaluated (Fastify with TypeBox is the closest, still less ergonomic for our use). Rejected.

### Java/Kotlin for services

Considered for the workflow integration angle (Temporal's reference SDK is Java). Rejected because of the operational footprint and the complete absence of Java in our target callers' agent stacks. Temporal has a Python SDK that is good enough for our needs.

### Single language for everything (including TS SDK in Python via Pyodide etc.)

Briefly considered, immediately rejected. The TS SDK has to be idiomatic TS for Node consumers. No shortcut here.

## Notes / Links

- Full conventions: [`CONVENTIONS.md`](../../CONVENTIONS.md)
- Service layout: [`docs/architecture/01-system-overview.md`](../architecture/01-system-overview.md)
- Future Rust hot-path: see Open Questions in [`docs/architecture/03-tool-gateway-design.md`](../architecture/03-tool-gateway-design.md)
- Multi-SDK strategy is also informed by ADR 0003 (MCP) — clients in any language can use Plinth via the HTTP API directly, the SDKs are ergonomic wrappers, not a hard gate.
