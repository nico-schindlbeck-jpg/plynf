# Architecture Decision Records

This directory contains the load-bearing decisions behind Plynf. Each ADR captures a single decision, its context, the alternatives considered, and the trade-offs we accepted. They are immutable once Accepted — superseding decisions get a new ADR that references the old one.

## Format

Every ADR follows the template:

```
# ADR NNNN: <Title>
- Status: Accepted | Proposed | Deprecated | Superseded by ADR-XXXX
- Date: YYYY-MM-DD
- Deciders: ...

## Context
## Decision
## Consequences (Positive / Negative-Trade-offs)
## Alternatives Considered
## Notes / Links
```

## Index

| # | Title | Status | One-liner |
|---|---|---|---|
| [0001](./0001-language-and-stack.md) | Language and stack | Accepted | Python 3.11+ for services and primary SDK; TypeScript SDK alongside; Rust deferred. |
| [0002](./0002-storage-postgres-and-objectstore.md) | Storage tiers | Accepted | SQLite for v0.1 PoC; Postgres + S3-compatible blob store for v1.0; metadata and blobs split intentionally. |
| [0003](./0003-mcp-as-tool-protocol.md) | MCP as tool protocol | Accepted | Adopt MCP as the tool exposure protocol; layer above it (gateway), don't compete; extend with caching/idempotency hints. |
| [0004](./0004-temporal-vs-custom-workflow.md) | Workflow engine choice | Proposed | Use Temporal as the durable execution engine for v0.3 coordination; Inngest as documented fallback. |
| [0005](./0005-bsl-vs-apache-licensing.md) | Licensing model | Accepted | Apache 2.0 for SDKs and v0.1 services; v1.0 production runtime moves to BSL with 4-year Apache transition. |
| [0006](./0006-multitenancy-model.md) | Multi-tenancy model | Proposed | v0.1 single-tenant; v0.2 shared schema with `tenant_id` and row-level security; v1.0 schema-per-tenant for enterprise. |

## Cross-references to architecture docs

ADRs commit to *what* and *why*; the architecture docs explain *how* and *what it means in practice*. Quick map:

- ADR 0001 → context for how everything else is implemented (`docs/architecture/01-system-overview.md`).
- ADR 0002 → backs the workspace storage model (`02-workspace-design.md`) and the gateway's audit/cache stores (`03-tool-gateway-design.md`).
- ADR 0003 → backs the gateway design (`03-tool-gateway-design.md`).
- ADR 0004 → executes the coordination sketch (`04-coordination-primitives.md`).
- ADR 0005 → no architectural impact; affects what we publish.
- ADR 0006 → backs the identity model (`06-identity-capabilities.md`) and the production deployment shape (`01-system-overview.md` §2.2).
