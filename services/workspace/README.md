# plinth-workspace

The **Workspace service** for [Plinth](../../README.md) — agent-native versioned
KV + file storage with snapshot and branch semantics.

It is a single FastAPI process that persists state to:

- **SQLite** at `$PLINTH_DATA_DIR/workspace.db` for workspace, KV, file, snapshot,
  and branch metadata.
- **Content-addressed blobs** at `$PLINTH_DATA_DIR/blobs/<workspace_id>/<sha256>`
  for file payloads.

Default `PLINTH_DATA_DIR=/tmp/plinth-data`.

## What it does

- **Workspaces** — top-level isolation boundary, each with metadata.
- **KV** — versioned JSON values per key. Every PUT creates a new immutable
  version; DELETE writes a tombstone version.
- **Files** — versioned blobs per path, content-addressed by sha256. Deduped
  inside a workspace.
- **Snapshots** — immutable point-in-time captures of the latest version of
  every key/file. Diff two snapshots to see what changed.
- **Branches** — divergent timelines started from a snapshot. Reads on a
  branch fall through to the from_snapshot's captured versions on main; writes
  stay on the branch until merged.
- **Channels (v0.2)** — typed, durable, per-workspace message queues for
  multi-agent handoffs. Lazy-created on first send, monotonic `seq` per
  channel, optional per-consumer cursors with `peek` support.
- **Workflows (v0.2)** — named manifests of step names plus a log of step
  attempts. Each step references a snapshot for resumability; `GET /resume`
  returns the next pending step + the latest captured snapshot id so an
  agent can pick up after a crash.

The full HTTP API surface mirrors `CONTRACTS.md → Workspace API`.

## Quickstart

```bash
# from services/workspace/
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# run the server
PLINTH_DATA_DIR=/tmp/plinth-data python -m plinth_workspace
# → uvicorn on http://0.0.0.0:7421

# health check
curl -s http://localhost:7421/healthz
# {"status":"ok","version":"0.1.0","service":"workspace"}

# create a workspace
curl -s -X POST http://localhost:7421/v1/workspaces \
  -H "Authorization: Bearer dev" \
  -H "Content-Type: application/json" \
  -d '{"name":"demo"}'
```

## Configuration

All env vars use the `PLINTH_` prefix:

| Variable | Default | Notes |
| --- | --- | --- |
| `PLINTH_DATA_DIR` | `/tmp/plinth-data` | Holds `workspace.db` and `blobs/`. |
| `PLINTH_WORKSPACE_PORT` | `7421` | uvicorn bind port. |
| `PLINTH_WORKSPACE_HOST` | `0.0.0.0` | uvicorn bind host. |
| `PLINTH_LOG_LEVEL` | `INFO` | Standard logging level. |
| `PLINTH_LOG_FORMAT` | `console` | `console` or `json`. |
| `PLINTH_AUTH_REQUIRED` | `false` | When `true`, returns 401 for missing token. |

## Tests

```bash
pytest -v --cov=plinth_workspace --cov-report=term-missing
```

Tests use a fresh `tmp_path`-backed data dir per test, run offline, and
should complete in well under 30 seconds.

## v0.2 — Channels and Workflows

### Channels

A **channel** is a workspace-scoped, durable, monotonic-sequence message
queue used for handoffs between agents (research → writer → reviewer
pipelines).

```bash
# Send (lazy-creates the channel)
curl -X POST "http://localhost:7421/v1/workspaces/$WS/channels/handoff/send" \
  -H "Authorization: Bearer dev" -H "content-type: application/json" \
  -d '{"payload":{"sources":[1,2]},"sender":"researcher"}'

# Receive (per-consumer cursor; peek to leave the cursor untouched)
curl "http://localhost:7421/v1/workspaces/$WS/channels/handoff/receive?consumer=writer&limit=10"
```

Endpoints:

```
POST   /v1/workspaces/{ws}/channels/{name}/send
GET    /v1/workspaces/{ws}/channels/{name}/receive?since=&limit=&consumer=&peek=
DELETE /v1/workspaces/{ws}/channels/{name}/messages/{message_id}
GET    /v1/workspaces/{ws}/channels
GET    /v1/workspaces/{ws}/channels/{name}
DELETE /v1/workspaces/{ws}/channels/{name}
```

Semantics:

- Channels are workspace-scoped and lazy-created on first `send`.
- `seq` is monotonic per channel.
- A `consumer` cursor advances on every non-peek receive; an explicit
  `since` overrides the cursor (rewind).
- `peek=true` returns messages without advancing the cursor or marking
  `delivered_at`.
- Default `limit=100`, max `1000`.

### Workflows

A **workflow** is a named manifest of step names plus a log of step
attempts. Each step has lifecycle `pending → running → completed | failed
| cancelled`. After a crash, an agent calls `/resume` to learn the next
pending step and the most recent snapshot id to restore from.

```bash
# Create with a manifest
WF=$(curl -X POST "http://localhost:7421/v1/workspaces/$WS/workflows" \
  -H "Authorization: Bearer dev" -H "content-type: application/json" \
  -d '{"name":"research","steps":["search","fetch","synth"]}' \
  | jq -r .id)

# Start a step
STEP=$(curl -X POST "http://localhost:7421/v1/workspaces/$WS/workflows/$WF/steps" \
  -H "Authorization: Bearer dev" -H "content-type: application/json" \
  -d '{"name":"search","input":{"q":"renewables"}}' | jq -r .id)

# Complete it (referencing a snapshot for resumability)
curl -X PATCH "http://localhost:7421/v1/workspaces/$WS/workflows/$WF/steps/$STEP" \
  -H "Authorization: Bearer dev" -H "content-type: application/json" \
  -d '{"status":"completed","snapshot_id":"snap_xyz","output":{"sources":[1,2]}}'

# After a crash: where do we pick up?
curl "http://localhost:7421/v1/workspaces/$WS/workflows/$WF/resume"
# → {"next_step":"fetch","snapshot_id":"snap_xyz", ...}
```

Endpoints:

```
POST   /v1/workspaces/{ws}/workflows
GET    /v1/workspaces/{ws}/workflows
GET    /v1/workspaces/{ws}/workflows/{wf_id}
POST   /v1/workspaces/{ws}/workflows/{wf_id}/steps
PATCH  /v1/workspaces/{ws}/workflows/{wf_id}/steps/{step_id}
GET    /v1/workspaces/{ws}/workflows/{wf_id}/resume
POST   /v1/workspaces/{ws}/workflows/{wf_id}/cancel
```

Semantics:

- A workflow's manifest is fixed at creation. Each step name must be in
  the manifest; re-starting a step under the same name allocates
  `attempt = max+1` so retries are observable.
- The workflow status is derived: `pending` (no steps yet) → `running`
  (first step started) → `completed` (every manifest entry has a
  completed attempt) or `failed` (a step failed and no later attempt
  has completed it). `cancelled` is sticky.
- `/resume` returns the first manifest entry that has no completed
  attempt, plus the snapshot id of the most recent completed step.

## Docker

```bash
docker build -t plinth-workspace .
docker run --rm -p 7421:7421 -v plinth-data:/data \
  -e PLINTH_DATA_DIR=/data plinth-workspace
```
