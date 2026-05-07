# Sequence — research agent demo

The reference demo (`examples/01-research-agent/`) uses every v0.1 primitive
end-to-end: it creates a workspace, searches the web (mocked), fetches each
result with caching, persists everything to KV and files, then snapshots the
result. This sequence shows how SDK calls translate to HTTP traffic against
the Workspace API and the Tool Gateway.

The first `web.fetch` for a given URL misses the cache and hits the upstream
mock MCP server. Subsequent identical fetches within the tool's
`cache_ttl_seconds` (default 300s) short-circuit at the gateway and never
touch the backend.

```mermaid
sequenceDiagram
    autonumber
    actor Agent as Agent code
    participant SDK as Plinth SDK
    participant WS as Workspace API\n:7421
    participant GW as Tool Gateway\n:7422
    participant MCP as mock-mcp\n:7423

    Agent->>SDK: client.workspace("research-task-1")
    SDK->>WS: POST /v1/workspaces\n{name: "research-task-1"}
    WS-->>SDK: 201 Workspace{id: ws_…}
    Note over SDK: ws_id cached in handle

    Agent->>SDK: tools.invoke("web.search", {query})
    SDK->>GW: POST /v1/invoke\n{tool_id, args, workspace_id}
    GW->>GW: cache lookup (miss)
    GW->>MCP: POST /invoke/web.search
    MCP-->>GW: {results: [5 sources]}
    GW->>GW: append AuditEvent\nstore cache entry
    GW-->>SDK: 200 InvokeResponse{result, cached:false}

    loop for each source[i] (i=1..5)
        Agent->>SDK: tools.invoke("web.fetch", {url})
        SDK->>GW: POST /v1/invoke
        alt cache hit (duplicate URL within TTL)
            GW-->>SDK: 200 InvokeResponse{cached:true}
        else cache miss
            GW->>MCP: POST /invoke/web.fetch
            MCP-->>GW: {content, status, content_type}
            GW->>GW: append AuditEvent\nstore cache entry
            GW-->>SDK: 200 InvokeResponse{cached:false}
        end
        Agent->>SDK: workspace.kv.set(f"sources/{url}", content)
        SDK->>WS: PUT /v1/workspaces/{ws_id}/kv/sources%2F…\n{value: content}
        WS-->>SDK: 200 KVEntry{version: N}
    end

    Agent->>Agent: synthesize report locally
    Agent->>SDK: workspace.files.write("report.md", body)
    SDK->>WS: PUT /v1/workspaces/{ws_id}/files/report.md
    WS-->>SDK: 200 FileEntry{version: 1}

    Agent->>SDK: workspace.snapshot("sources-collected")
    SDK->>WS: POST /v1/workspaces/{ws_id}/snapshots\n{name}
    WS-->>SDK: 201 Snapshot{id: snap_…}
    SDK-->>Agent: Snapshot
```

## Key invariants illustrated

- **Workspace handle is sticky.** All subsequent state ops carry `ws_id`.
- **Audit precedes cache write.** Even cached calls go through the audit log.
- **Workspace state is independent of the gateway.** A failed fetch leaves a
  consistent KV/file timeline that can be rolled back via snapshot.
