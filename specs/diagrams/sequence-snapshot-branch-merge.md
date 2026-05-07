# Sequence — snapshot, branch, merge

This sequence shows the canonical "what-if" exploration pattern an agent uses
to try a risky mutation in isolation, decide whether the result is good, and
then either merge it back into the main timeline or discard it. All requests
hit the Workspace API at `:7421`.

Branch reads see *branch-specific writes first*, then fall through to the
source snapshot. Branch writes never affect the parent timeline until merge.

```mermaid
sequenceDiagram
    autonumber
    actor Agent as Agent code
    participant SDK as Plinth SDK
    participant WS as Workspace API\n:7421
    participant DB as SQLite + blobs

    Note over Agent,DB: Workspace "ws_alpha" already exists with kv: {topic: "v1"}

    Agent->>SDK: ws.snapshot("baseline", "before experiment")
    SDK->>WS: POST /v1/workspaces/ws_alpha/snapshots\n{name, message}
    WS->>DB: capture latest versions of all KV+files
    WS-->>SDK: 201 Snapshot{id: snap_base, kv_versions, file_versions}

    Agent->>SDK: ws.branch("experiment", from_snapshot=snap_base)
    SDK->>WS: POST /v1/workspaces/ws_alpha/branches\n{name, from_snapshot}
    WS->>DB: insert Branch row (merged=false)
    WS-->>SDK: 201 Branch{id: br_exp}

    Agent->>SDK: ws.with_branch(br_exp).kv.set("topic", "v2")
    SDK->>WS: PUT /v1/workspaces/ws_alpha/kv/topic?branch=br_exp\n{value: "v2"}
    WS->>DB: insert KVEntry{branch_id: br_exp, version: N+1}
    WS-->>SDK: 200 KVEntry

    Agent->>SDK: ws.with_branch(br_exp).kv.get("topic")
    SDK->>WS: GET /v1/workspaces/ws_alpha/kv/topic?branch=br_exp
    Note right of WS: branch overlay → fall-through to snap_base
    WS-->>SDK: 200 KVEntry{value: "v2"}
    Agent->>SDK: ws.kv.get("topic")\n# main timeline
    SDK->>WS: GET /v1/workspaces/ws_alpha/kv/topic
    WS-->>SDK: 200 KVEntry{value: "v1"}

    Agent->>SDK: ws.snapshot("experiment-tip", branch=br_exp)
    SDK->>WS: POST /v1/workspaces/ws_alpha/snapshots?branch=br_exp\n{name}
    WS-->>SDK: 201 Snapshot{id: snap_tip}

    Agent->>SDK: ws.diff(snap_base, snap_tip)
    SDK->>WS: GET /v1/workspaces/ws_alpha/snapshots/snap_base/diff?against=snap_tip
    WS-->>SDK: 200 DiffResult{kv_modified: ["topic"], …}

    alt agent decides to keep the branch
        Agent->>SDK: ws.merge(br_exp)
        SDK->>WS: POST /v1/workspaces/ws_alpha/branches/br_exp/merge
        WS->>DB: apply branch writes to main timeline\nmark Branch.merged=true
        WS->>DB: create new Snapshot on parent (post-merge)
        WS-->>SDK: 200 MergeResult{branch_id, snapshot_id, diff}
    else agent discards
        Agent->>SDK: ws.delete_branch(br_exp)
        SDK->>WS: DELETE /v1/workspaces/ws_alpha/branches/br_exp
        WS->>DB: tombstone branch entries
        WS-->>SDK: 204
    end
```

## Notes

- A snapshot is metadata only — it just records `{key → version}` and
  `{path → version}` for every entry already present.
- Merging produces a **new snapshot** on the parent timeline. The branch row
  is marked `merged=true` but kept for audit.
- Discard is non-destructive in v0.1 (we tombstone, not vacuum) so audit
  trails stay intact.
