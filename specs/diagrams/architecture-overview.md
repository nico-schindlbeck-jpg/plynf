# Architecture overview

This diagram shows the runtime topology of Plinth at v0.1 plus the post-v0.1
extensions sketched in `ARCHITECTURE.md`. Solid boxes ship in v0.1; dashed
boxes are stubs scheduled for v0.2.

The Agent SDK is the only thing the agent code talks to directly. The SDK fans
out to the Workspace API for state and to the Tool Gateway for tool calls. The
two services share a data directory but no in-process state, so they can be
deployed independently.

```mermaid
flowchart LR
    classDef shipped fill:#e8f4ff,stroke:#1e6fd9,color:#0b3d91,stroke-width:1.5px;
    classDef future fill:#fff7e6,stroke:#b07d00,color:#5a3e00,stroke-dasharray: 5 4,stroke-width:1.5px;
    classDef storage fill:#f0fff0,stroke:#137333,color:#0d4e1f,stroke-width:1.5px;
    classDef external fill:#f5f5f5,stroke:#444,color:#222,stroke-width:1.2px;

    Agent["Agent code\n(Python / TypeScript)"]:::external
    SDK["Plinth SDK\n@plinth/sdk · plinth"]:::shipped

    subgraph Services["Plinth services"]
        Workspace["Workspace API\n:7421\nFastAPI"]:::shipped
        Gateway["Tool Gateway\n:7422\nFastAPI"]:::shipped
        Coord["Coordination [v0.2]\nChannels · Locks · Workflows"]:::future
        Obs["Observability [v0.2]\nUnified event stream"]:::future
        Identity["Identity [v0.2]\nCapability tokens"]:::future
    end

    subgraph Storage["Storage"]
        WSDB[("workspace.db\nSQLite")]:::storage
        Blobs[("blobs/\nfilesystem")]:::storage
        GWDB[("gateway.db\nSQLite\naudit + cache")]:::storage
    end

    subgraph Tools["Tool backends"]
        MCP["MCP servers\n(real or mock-mcp:7423)"]:::external
        HTTP["HTTP / GraphQL APIs"]:::external
    end

    Agent --> SDK
    SDK -->|REST /v1/workspaces| Workspace
    SDK -->|REST /v1/invoke| Gateway
    SDK -.->|v0.2 channels/locks| Coord
    SDK -.->|v0.2 capability tokens| Identity

    Workspace --> WSDB
    Workspace --> Blobs
    Gateway --> GWDB
    Gateway --> MCP
    Gateway --> HTTP

    Workspace -.->|emit events| Obs
    Gateway -.->|emit events| Obs
    Coord -.->|emit events| Obs
    Identity -.->|verify token| Gateway
    Identity -.->|verify token| Workspace
```

## Component responsibilities

| Component | Status | Responsibility |
|-----------|--------|----------------|
| Agent SDK | v0.1 | Ergonomic client; hides REST details; powers `@client.agent` decorator. |
| Workspace API | v0.1 | Versioned KV, files, snapshots, branches, merge/diff. |
| Tool Gateway | v0.1 | Tool registry, invoke proxy, cache, idempotency, dry-run, audit log. |
| `mock-mcp` | v0.1 | Demo MCP server with offline fixtures (`web.fetch`, `web.search`, etc.). |
| Coordination | v0.2 | Channels, locks, durable workflows (Temporal-backed). |
| Observability | v0.2 | OTLP-compatible unified semantic event stream. |
| Identity | v0.2 | Capability-token issuance and verification. |
