# Plynf — Internal API Contracts

> **For implementers**: This document is the source of truth for inter-service contracts. All services and SDKs MUST conform to it. If you must deviate, update this file FIRST and note it in your PR.

## Service Topology

| Service | Port | Process | Storage |
|---------|------|---------|---------|
| `workspace` | **7421** | FastAPI (uvicorn) | SQLite at `$PLINTH_DATA_DIR/workspace.db`, blobs at `$PLINTH_DATA_DIR/blobs/` |
| `gateway` | **7422** | FastAPI (uvicorn) | SQLite at `$PLINTH_DATA_DIR/gateway.db` (audit + cache + tokens) |
| `mock-mcp` | **7423** | FastAPI (uvicorn) | In-memory + `examples/fixtures/` |

Default `PLINTH_DATA_DIR=/tmp/plinth-data` (overridable via env).

All services expose `GET /healthz` returning `{"status": "ok", "version": "0.1.0"}`.

---

## Workspace API (`http://localhost:7421`)

Authentication: `Authorization: Bearer <token>` — in PoC, any non-empty token; the gateway will issue scoped tokens later.

### Resources

#### Workspace
A workspace is the top-level isolation boundary for an agent's state.

```
POST   /v1/workspaces                          → 201 {Workspace}
GET    /v1/workspaces                          → 200 {workspaces: [Workspace]}
GET    /v1/workspaces/{ws_id}                  → 200 {Workspace}
DELETE /v1/workspaces/{ws_id}                  → 204
```

#### KV — versioned key-value
Every PUT creates a new immutable version. GET returns latest by default; `?version=N` returns specific version.

```
PUT    /v1/workspaces/{ws_id}/kv/{key}         body: {value: any} → 200 {KVEntry}
GET    /v1/workspaces/{ws_id}/kv/{key}         ?version=N         → 200 {KVEntry}
DELETE /v1/workspaces/{ws_id}/kv/{key}                            → 204 (creates tombstone version)
GET    /v1/workspaces/{ws_id}/kv/{key}/history                    → 200 {versions: [KVEntry]}
GET    /v1/workspaces/{ws_id}/kv                                   → 200 {entries: [KVEntry]}  (latest of each)
```

#### Files — versioned blob storage
Same versioning model as KV but for binary/text content with paths.

```
PUT    /v1/workspaces/{ws_id}/files/{path:path}  body: bytes      → 200 {FileEntry}
GET    /v1/workspaces/{ws_id}/files/{path:path}  ?version=N       → 200 (raw bytes)
GET    /v1/workspaces/{ws_id}/files/{path:path}/meta              → 200 {FileEntry}
DELETE /v1/workspaces/{ws_id}/files/{path:path}                   → 204
GET    /v1/workspaces/{ws_id}/files                                → 200 {files: [FileEntry]}
```

#### Snapshots — immutable point-in-time
A snapshot captures the current latest version of every key/file in the workspace.

```
POST   /v1/workspaces/{ws_id}/snapshots        body: {name, message?} → 201 {Snapshot}
GET    /v1/workspaces/{ws_id}/snapshots                                → 200 {snapshots: [Snapshot]}
GET    /v1/workspaces/{ws_id}/snapshots/{snap_id}                      → 200 {Snapshot}
GET    /v1/workspaces/{ws_id}/snapshots/{snap_id}/diff?against={snap_id2} → 200 {DiffResult}
```

#### Branches — divergent timelines
A branch is a working copy created from a snapshot. Writes to a branch don't affect main until merged.

```
POST   /v1/workspaces/{ws_id}/branches         body: {name, from_snapshot} → 201 {Branch}
GET    /v1/workspaces/{ws_id}/branches                                     → 200 {branches: [Branch]}
POST   /v1/workspaces/{ws_id}/branches/{branch_id}/merge                   → 200 {merge_result}
DELETE /v1/workspaces/{ws_id}/branches/{branch_id}                         → 204
```
All KV/file/snapshot endpoints accept `?branch={branch_id}` to target a specific branch.

### Pydantic Models

```python
class Workspace(BaseModel):
    id: str               # ws_<ulid>
    name: str
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = {}

class KVEntry(BaseModel):
    workspace_id: str
    key: str
    value: Any            # JSON-serializable
    version: int          # monotonic per (workspace, key)
    created_at: datetime
    deleted: bool = False
    branch_id: str | None = None

class FileEntry(BaseModel):
    workspace_id: str
    path: str
    size: int
    sha256: str
    content_type: str
    version: int
    created_at: datetime
    deleted: bool = False
    branch_id: str | None = None

class Snapshot(BaseModel):
    id: str               # snap_<ulid>
    workspace_id: str
    name: str
    message: str | None = None
    created_at: datetime
    kv_versions: dict[str, int]    # key → version
    file_versions: dict[str, int]  # path → version
    parent_snapshot_id: str | None = None

class Branch(BaseModel):
    id: str               # br_<ulid>
    workspace_id: str
    name: str
    from_snapshot_id: str
    created_at: datetime
    merged: bool = False
    merged_at: datetime | None = None

class DiffResult(BaseModel):
    kv_added: list[str]
    kv_modified: list[str]
    kv_deleted: list[str]
    files_added: list[str]
    files_modified: list[str]
    files_deleted: list[str]
```

---

## Gateway API (`http://localhost:7422`)

### Resources

#### Tools — registered MCP servers / tools
```
POST   /v1/tools/register      body: {ToolRegistration}           → 201 {Tool}
GET    /v1/tools                                                  → 200 {tools: [Tool]}
GET    /v1/tools/{tool_id}                                        → 200 {Tool}
DELETE /v1/tools/{tool_id}                                        → 204
```

#### Invoke — call a tool with caching/audit
```
POST   /v1/invoke              body: {InvokeRequest}              → 200 {InvokeResponse}
POST   /v1/invoke/dry-run      body: {InvokeRequest}              → 200 {DryRunResponse}
```

#### Audit — query log of all calls
```
GET    /v1/audit               ?workspace_id=&tool_id=&since=&limit=  → 200 {events: [AuditEvent]}
GET    /v1/audit/stats         ?workspace_id=                        → 200 {stats: {...}}
```

#### Cache — inspect / clear cache
```
GET    /v1/cache/stats                                                → 200 {hits, misses, size_bytes}
DELETE /v1/cache               ?tool_id=                              → 204
```

### Pydantic Models

```python
class ToolRegistration(BaseModel):
    tool_id: str          # e.g. "web.fetch", "fs.read"
    name: str
    description: str      # agent-optimized: short, action-oriented
    transport: Literal["http", "stdio"]
    endpoint: str         # URL for http, command for stdio
    input_schema: dict    # JSON Schema
    output_schema: dict   # JSON Schema
    idempotent: bool = False
    side_effects: Literal["none", "read", "write"] = "read"
    cache_ttl_seconds: int | None = 300  # null = no caching
    auth_method: Literal["none", "bearer", "oauth2"] = "none"
    auth_config: dict[str, Any] = {}

class Tool(ToolRegistration):
    created_at: datetime
    updated_at: datetime

class InvokeRequest(BaseModel):
    tool_id: str
    arguments: dict[str, Any]
    workspace_id: str | None = None    # for audit attribution
    agent_id: str | None = None        # for audit attribution
    cache: bool = True
    idempotency_key: str | None = None

class InvokeResponse(BaseModel):
    tool_id: str
    arguments: dict[str, Any]
    result: Any
    cached: bool
    duration_ms: int
    audit_id: str
    cost_estimate_usd: float = 0.0

class DryRunResponse(BaseModel):
    tool_id: str
    arguments: dict[str, Any]
    would_invoke: bool
    cached_result: Any | None = None
    estimated_cost_usd: float = 0.0
    estimated_duration_ms: int = 0

class AuditEvent(BaseModel):
    id: str               # evt_<ulid>
    timestamp: datetime
    tool_id: str
    workspace_id: str | None
    agent_id: str | None
    arguments_hash: str
    result_hash: str
    cached: bool
    duration_ms: int
    cost_estimate_usd: float
    error: str | None = None
```

---

## Mock MCP Server API (`http://localhost:7423`)

A minimal MCP-style server exposing 5 demo tools:

```
GET    /tools                                  → 200 {tools: [...]}    # tool list
POST   /invoke/{tool_name}    body: {args}     → 200 {result}
```

### Tools provided
| name | description | input | output |
|------|-------------|-------|--------|
| `web.fetch` | Fetch a URL and return its text content. | `{url: str}` | `{content: str, status: int, content_type: str}` |
| `web.search` | Mock web search returning canned results from fixtures. | `{query: str, k: int = 5}` | `{results: [{title, url, snippet}]}` |
| `fs.read` | Read a file relative to the mock fixtures dir. | `{path: str}` | `{content: str}` |
| `fs.write` | Write a file relative to the mock fixtures dir. | `{path: str, content: str}` | `{path: str, bytes_written: int}` |
| `notes.add` | Append a note (in-memory, per-session). | `{title: str, body: str}` | `{id: str}` |
| `notes.list` | List notes. | `{}` | `{notes: [...]}` |

The fetch tool has built-in fixtures: URLs starting with `mock://` return canned content (so demos run offline). Real `https://` URLs use httpx.

---

## SDK Surface (Python)

```python
from plinth import Plynf

client = Plynf(
    workspace_url="http://localhost:7421",
    gateway_url="http://localhost:7422",
    api_key="local-dev",
)

# Workspace
ws = client.workspace("research-task-1")          # get-or-create
ws.kv.set("topic", "renewable energy")            # versioned write
value, version = ws.kv.get("topic", with_version=True)
history = ws.kv.history("topic")                  # all versions
ws.files.write("report.md", "# Report\n...")
content = ws.files.read("report.md")

snap = ws.snapshot("baseline", message="initial state")
branch = ws.branch("experiment", from_snapshot=snap.id)
ws.with_branch(branch.id).kv.set("topic", "...")  # writes go to branch
diff = ws.diff(snap.id, branch.head_snapshot_id)

# Tools
tools = client.tools
result = tools.invoke("web.fetch", {"url": "..."})       # cached automatically
audit = tools.audit(workspace_id=ws.id, since="1h")

# Token counting (cl100k tiktoken-compatible, offline)
tokens = client.count_tokens("...")

# Agent helper
@client.agent(workspace="research-task-1")
def my_agent(ctx, topic: str):
    sources = ctx.tools.invoke("web.search", {"query": topic})
    for src in sources["results"]:
        content = ctx.tools.invoke("web.fetch", {"url": src["url"]})  # cached
        ctx.workspace.kv.set(f"sources/{src['url']}", content)
    ctx.workspace.snapshot("sources-collected")
```

---

## SDK Surface (TypeScript)

Mirrors Python where ergonomic. Subset for v0.1:

```typescript
import { Plynf } from "@plinth/sdk";

const client = new Plynf({
  workspaceUrl: "http://localhost:7421",
  gatewayUrl: "http://localhost:7422",
  apiKey: "local-dev",
});

const ws = await client.workspace("research-task-1");
await ws.kv.set("topic", "renewable energy");
const result = await client.tools.invoke("web.fetch", { url: "..." });
const snap = await ws.snapshot("baseline");
```

---

## Error Model

All errors return:
```json
{
  "error": {
    "code": "WORKSPACE_NOT_FOUND",
    "message": "Workspace ws_xyz does not exist",
    "details": {...}
  }
}
```

Standard codes:
- `WORKSPACE_NOT_FOUND`, `KEY_NOT_FOUND`, `FILE_NOT_FOUND`, `SNAPSHOT_NOT_FOUND`, `BRANCH_NOT_FOUND`
- `TOOL_NOT_FOUND`, `TOOL_INVOCATION_FAILED`, `INVALID_ARGUMENTS`
- `UNAUTHORIZED`, `RATE_LIMITED`, `INTERNAL_ERROR`

HTTP status: 400 (validation), 401 (auth), 404 (not found), 429 (rate), 500 (internal).

---

## Versioning

API version `v1` lives at `/v1/...`. Breaking changes go to `/v2/...`. Pre-1.0 (now), backward incompat allowed within v1 with major version bump in package.

---

# v0.2 Additions

The following endpoints/types are added for the v0.2 MVP. They are additive — v0.1 endpoints remain unchanged.

## Channels API (Workspace service)

A **channel** is a typed, persistent message queue inside a workspace. Used for hand-offs between agents (research → writer → reviewer pipelines).

### Endpoints

```
POST   /v1/workspaces/{ws_id}/channels/{name}/send       body: ChannelSendBody  → 201 ChannelMessage
GET    /v1/workspaces/{ws_id}/channels/{name}/receive    ?since=&limit=&consumer=&peek=  → 200 {messages: [ChannelMessage]}
DELETE /v1/workspaces/{ws_id}/channels/{name}/messages/{message_id}  → 204  (ack/delete)
GET    /v1/workspaces/{ws_id}/channels                                → 200 {channels: [Channel]}
GET    /v1/workspaces/{ws_id}/channels/{name}                         → 200 Channel
DELETE /v1/workspaces/{ws_id}/channels/{name}                         → 204
```

### Semantics

- **Channels are workspace-scoped** — created lazily on first `send`.
- **Messages are durable** — persisted to SQLite; survive restarts.
- **Ordering**: monotonic ULID sequence per channel.
- **`receive`** returns all messages with `seq > since`; default `since=0` returns from start.
  - `consumer=<name>` (optional): server tracks per-consumer cursor; subsequent calls without `since` resume from cursor.
  - `peek=true`: don't advance cursor.
  - `limit`: max messages (default 100, max 1000).
- **Ack/delete** removes a message (or just advances cursor if you prefer cursor-based).

### Models

```python
class ChannelSendBody(BaseModel):
    payload: Any                      # JSON-serializable
    sender: str | None = None         # agent_id or descriptive label
    type: str | None = None           # optional message type ("research-complete")
    correlation_id: str | None = None # for request/response correlation
    headers: dict[str, str] = {}

class ChannelMessage(BaseModel):
    id: str                           # msg_<ulid>
    channel: str
    workspace_id: str
    seq: int                          # monotonic per channel
    payload: Any
    sender: str | None = None
    type: str | None = None
    correlation_id: str | None = None
    headers: dict[str, str] = {}
    sent_at: datetime
    delivered_at: datetime | None = None  # set when first received

class Channel(BaseModel):
    name: str
    workspace_id: str
    message_count: int                # current depth
    created_at: datetime
    last_send_at: datetime | None = None
    last_receive_at: datetime | None = None
```

ID prefix: `msg_<ulid>`.

## Workflows API (Workspace service)

A **workflow** is a named sequence of agent steps with checkpointed state. Each step references a snapshot for resumability.

### Endpoints

```
POST   /v1/workspaces/{ws_id}/workflows                              body: WorkflowCreate  → 201 Workflow
GET    /v1/workspaces/{ws_id}/workflows                                                    → 200 {workflows: [Workflow]}
GET    /v1/workspaces/{ws_id}/workflows/{wf_id}                                            → 200 Workflow (with steps)
POST   /v1/workspaces/{ws_id}/workflows/{wf_id}/steps                body: WorkflowStepCreate → 201 WorkflowStep
PATCH  /v1/workspaces/{ws_id}/workflows/{wf_id}/steps/{step_id}      body: WorkflowStepUpdate → 200 WorkflowStep
GET    /v1/workspaces/{ws_id}/workflows/{wf_id}/resume               → 200 {next_step: str | None, last_completed: WorkflowStep | None, snapshot_id: str | None}
POST   /v1/workspaces/{ws_id}/workflows/{wf_id}/cancel                                     → 200 Workflow
```

### Semantics

- A workflow is a **manifest** of expected step names plus a log of completed steps.
- Each step has lifecycle: `pending → running → completed | failed | cancelled`.
- On step completion, agent should snapshot the workspace and reference that snapshot in the step.
- `GET /resume` returns: name of next pending step + snapshot_id of the most recent completed step.
- Crash → restart agent → call `/resume` → pick up from where you left off.

### Models

```python
class WorkflowCreate(BaseModel):
    name: str
    steps: list[str]                  # ordered list of step names (the manifest)
    metadata: dict[str, Any] = {}

class WorkflowStepCreate(BaseModel):
    name: str                         # must be one of workflow.steps
    snapshot_id: str | None = None
    input: Any | None = None

class WorkflowStepUpdate(BaseModel):
    status: Literal["running", "completed", "failed", "cancelled"]
    output: Any | None = None
    error: str | None = None
    snapshot_id: str | None = None

class WorkflowStep(BaseModel):
    id: str                           # step_<ulid>
    workflow_id: str
    name: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    input: Any | None = None
    output: Any | None = None
    error: str | None = None
    snapshot_id: str | None = None    # captured workspace state at step boundary
    attempt: int = 1

class Workflow(BaseModel):
    id: str                           # wf_<ulid>
    workspace_id: str
    name: str
    steps_manifest: list[str]         # the expected steps
    steps: list[WorkflowStep]         # the actual log
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    metadata: dict[str, Any] = {}

class ResumeInfo(BaseModel):
    workflow_id: str
    workflow_status: str
    next_step: str | None             # None if all done
    last_completed: WorkflowStep | None
    snapshot_id: str | None           # snapshot to restore from
```

ID prefixes: `wf_<ulid>`, `step_<ulid>`.

## Rate Limiting & Cost Caps (Gateway service)

The gateway enforces per-`agent_id` (or per-`workspace_id`) rate limits and cost ceilings.

### Configuration via env vars

```
PLINTH_RATE_LIMIT_DEFAULT_RPM      = 60      # calls per minute (default)
PLINTH_RATE_LIMIT_DEFAULT_BURST    = 20      # burst allowance
PLINTH_COST_CAP_DEFAULT_USD_HOUR   = 1.0     # max $ per hour per agent
PLINTH_COST_CAP_DEFAULT_USD_DAY    = 10.0    # max $ per day per agent
```

Per-agent overrides via:
- `POST /v1/limits/{agent_id}  body: AgentLimits  → 200 AgentLimits`
- `GET  /v1/limits/{agent_id}                     → 200 AgentLimits`
- `DELETE /v1/limits/{agent_id}                   → 204`

### Enforcement

On every `POST /v1/invoke`:
1. Check rate (token bucket per agent_id)
2. Check rolling-window cost (1h + 24h)
3. If either exceeded → return 429 with retry-after info

### New Models

```python
class AgentLimits(BaseModel):
    agent_id: str
    rpm: int = 60                # calls per minute
    burst: int = 20
    cost_cap_usd_hour: float = 1.0
    cost_cap_usd_day: float = 10.0
    updated_at: datetime

class LimitsStatus(BaseModel):
    agent_id: str
    rpm_limit: int
    rpm_used_in_window: int
    cost_cap_usd_hour: float
    cost_used_usd_hour: float
    cost_cap_usd_day: float
    cost_used_usd_day: float
```

`GET /v1/limits/{agent_id}/status → LimitsStatus`

### 429 Response

```json
{
  "error": {
    "code": "RATE_LIMITED",
    "message": "Rate limit exceeded for agent_id agt_X. Retry after 12s.",
    "details": {
      "limit_type": "rpm" | "cost_hour" | "cost_day",
      "retry_after_seconds": 12,
      "current": 60,
      "limit": 60
    }
  }
}
```

`Retry-After` HTTP header also set.

## Dashboard service (new — port 7424)

Static SPA + minimal proxy that polls workspace + gateway APIs and renders the system state.

### Endpoints

- `GET /` → HTML dashboard
- `GET /api/overview` → aggregated read-only summary (workspaces, channels, workflows, recent audit, cost rollup)
- `GET /healthz`

The dashboard is **read-only** in v0.2 — no mutations. Pure observability + introspection.

## Python SDK additions

```python
# Channels
ws.channels.send("research-out", {"sources": [...]}, sender="researcher")
messages = ws.channels.receive("research-out", consumer="writer", limit=10)
for msg in messages:
    process(msg.payload)
    ws.channels.ack(msg)
ws.channels.list()                    # → list[Channel]

# Workflows
wf = ws.workflows.create("research-pipeline", steps=["search", "fetch", "extract", "synthesize"])
step = wf.start_step("search", input={"topic": "renewable energy"})
# … work happens, workspace mutates …
snap = ws.snapshot("after-search")
wf.complete_step(step.id, output={"sources": [...]}, snapshot_id=snap.id)

# Resume after crash
wf2 = ws.workflows.get(wf.id)
resume = wf2.resume_info()
if resume.next_step:
    # restore from resume.snapshot_id, then continue at resume.next_step
    ...
```

## TypeScript SDK additions

Mirror the Python SDK surface. The minimum useful subset for v0.2: send/receive on channels; create/start_step/complete_step on workflows.

---

# v0.3 Additions

The v0.3 release focuses on production-credibility: real authentication, multi-tenancy, and one real OAuth-backed tool integration.

## Identity Service (new — port 7425)

A new service that issues, verifies, and revokes JWT capability tokens. Workspace and Gateway delegate auth to it (or verify locally with the public key for performance).

### Endpoints

```
POST /v1/tokens                                  body: TokenIssueRequest  → 201 TokenIssueResponse
POST /v1/tokens/verify                           body: {token: str}       → 200 TokenClaims | 401
POST /v1/tokens/{jti}/revoke                                              → 204
GET  /v1/tokens/{jti}                                                     → 200 TokenInfo (without secret)
GET  /v1/.well-known/jwks.json                                            → 200 JWKS for public-key verification
GET  /healthz
```

### Models

```python
class TokenIssueRequest(BaseModel):
    agent_id: str
    tenant_id: str = "default"
    scopes: list[str]              # e.g. ["tool:web.fetch:read", "workspace:ws_x:write"]
    workspace_id: str | None = None
    ttl_seconds: int = 3600
    metadata: dict[str, Any] = {}

class TokenIssueResponse(BaseModel):
    token: str                     # JWT
    jti: str                       # token ID for revocation
    expires_at: datetime
    claims: TokenClaims

class TokenClaims(BaseModel):
    sub: str                       # = agent_id
    iss: str                       # = identity service URL
    aud: str                       # = "plinth"
    iat: int
    exp: int
    jti: str
    agent_id: str
    tenant_id: str
    workspace_id: str | None = None
    scopes: list[str]
    rate_limit: dict | None = None # may carry per-token RPM/cost overrides

class TokenInfo(BaseModel):
    jti: str
    agent_id: str
    tenant_id: str
    issued_at: datetime
    expires_at: datetime
    revoked: bool
    revoked_at: datetime | None = None
    metadata: dict
```

### JWT format

- Algorithm: **HS256** (shared secret) for v0.3 simplicity. Production should use RS256 with key rotation.
- Secret env: `PLINTH_IDENTITY_JWT_SECRET` (32+ random bytes, base64-encoded)
- Issuer: `iss = "http://localhost:7425"` (or the identity URL)

### Scope grammar

- `tool:<tool_id>` — invoke any operation on this tool
- `tool:<tool_id>:read` / `:write` / `:execute` — restrict to side-effect class
- `workspace:<ws_id>:read` / `:write` / `:admin`
- `tenant:<tenant_id>:admin` — manage tokens within tenant
- `*` — superuser (only at issuance time, never via UI)

### Revocation

- Maintained as `revoked_jtis` table in identity DB.
- Workspace + Gateway poll the identity service every 60s for newly revoked JTIs (or check on each request — configurable; default poll for performance).

## Multi-Tenancy Across Services

### Workspace + Gateway changes

Add `tenant_id TEXT NOT NULL DEFAULT 'default'` column to:
- `workspaces` table
- `audit_events` table
- `agent_limits` table

Auth middleware extracts `tenant_id` from the verified JWT claim. Falls back to `"default"` if no token (PoC compatibility).

All list/query endpoints filter by `tenant_id` — workspaces in tenant A invisible to tokens in tenant B.

### New endpoints

```
GET /v1/tenants                              → list tenants visible to caller
                                              (workspace + gateway both implement)
```

## OAuth 2.0 Authorization Code Flow (Gateway)

Real OAuth integration — **GitHub** as the first provider in v0.3. Generic enough that other providers (Slack, Linear) drop in.

### Endpoints (Gateway)

```
GET  /v1/oauth/{provider}/authorize           ?redirect_uri=&state=&scopes=  → 302 to provider
GET  /v1/oauth/{provider}/callback            ?code=&state=                   → 302 back to redirect_uri with token grant
POST /v1/oauth/{provider}/refresh             body: {connection_id}           → new access token

POST /v1/oauth/connections                    body: ConnectionCreate          → 201 OAuthConnection
GET  /v1/oauth/connections                    ?tenant_id=                     → list
GET  /v1/oauth/connections/{conn_id}                                          → OAuthConnection
DELETE /v1/oauth/connections/{conn_id}                                        → 204 (revokes locally + at provider best-effort)
```

### Models

```python
class OAuthProvider(BaseModel):
    name: str                            # "github" | "slack" | "linear"
    client_id: str
    authorize_url: str
    token_url: str
    userinfo_url: str | None = None
    default_scopes: list[str]
    pkce: bool = True

class OAuthConnection(BaseModel):
    id: str                              # conn_<ulid>
    tenant_id: str
    provider: str                        # "github"
    user_id: str                         # provider's user identifier
    user_login: str | None = None
    scopes: list[str]
    access_token_encrypted: str          # NEVER returned in API responses; placeholder
    expires_at: datetime | None = None
    refresh_token_encrypted: str | None = None
    created_at: datetime
    last_refreshed_at: datetime | None = None

class OAuthConnectionPublic(BaseModel):
    """API-safe view (no tokens)."""
    id: str
    tenant_id: str
    provider: str
    user_login: str | None
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None
```

### Configuration

```
PLINTH_OAUTH_GITHUB_CLIENT_ID=...
PLINTH_OAUTH_GITHUB_CLIENT_SECRET=...
PLINTH_OAUTH_GITHUB_REDIRECT_URI=http://localhost:7422/v1/oauth/github/callback
PLINTH_OAUTH_GITHUB_SCOPES="repo,read:user"
PLINTH_OAUTH_ENCRYPTION_KEY=...           # 32-byte base64 — used to encrypt at-rest tokens
```

### Tool registration with OAuth

When a tool registers with `auth_method=oauth2`, its `auth_config` references a connection_id template:
```json
{"auth_method": "oauth2", "auth_config": {"provider": "github", "connection_id_from": "agent.identity.workspace_id"}}
```

The gateway, on invoke, looks up the matching connection and attaches `Authorization: Bearer <decrypted_access_token>` to the outbound HTTP call.

## GitHub MCP server (new — port 7426)

A minimal real-MCP server that calls the GitHub REST API. Runs locally; uses the access token forwarded by the gateway.

### Tools

| tool_id | what it does | scopes required |
|---------|--------------|-----------------|
| `github.list_issues` | List issues for a repo | `repo` |
| `github.get_issue` | Get full issue details + comments | `repo` |
| `github.create_issue` | Create new issue | `repo` |
| `github.update_issue` | Edit title/body/labels/state | `repo` |
| `github.comment_on_issue` | Add comment | `repo` |
| `github.get_repo` | Repo metadata | `repo` |
| `github.search_code` | Code search within repo | `repo` |

Token is read from the `Authorization: Bearer ...` header forwarded by the gateway.

Health: `GET /healthz`

## SDK additions (Python)

```python
# Authenticate
client = Plynf(
    workspace_url=...,
    gateway_url=...,
    identity_url="http://localhost:7425",
    api_key="...",  # initial bootstrap token, OR:
)
# or get a capability token:
token = client.identity.issue_token(
    agent_id="my-agent",
    scopes=["tool:web.fetch:read", "workspace:my-task:write"],
    ttl_seconds=3600,
)
client_with_token = Plynf(workspace_url=..., gateway_url=..., api_key=token.token)

# OAuth (server-side)
auth_url = client.gateway.oauth_authorize_url("github", redirect_uri="...", scopes=["repo"])
# user visits auth_url, returns to redirect_uri with ?code=...
conn = client.gateway.oauth_complete("github", code="...", state="...")
# now register a tool that uses this connection
```

## Dashboard updates

- Show tenants: `GET /api/tenants`
- Show OAuth connections per tenant
- Show active capability tokens per agent (id, scopes, expiry; secrets never displayed)
- Filter audit/cost views by tenant

---

# v0.4 Additions

The v0.4 release focuses on scale and operability: a real database backend, observability via OTLP, more OAuth providers, and RS256 with key rotation.

## Postgres Backend (Workspace + Gateway + Identity)

Each service supports two storage drivers selected at startup:
- **`sqlite`** (default, v0.1+): single-file local DB
- **`postgres`** (new in v0.4): for production scale-out

### Configuration

```
PLINTH_STORAGE_DRIVER=sqlite|postgres        # default sqlite
PLINTH_DATABASE_URL=postgresql+asyncpg://user:pw@host:5432/plinth
                                              # required when driver=postgres
```

Per-service env vars override:
- `PLINTH_WORKSPACE_DATABASE_URL`
- `PLINTH_GATEWAY_DATABASE_URL`
- `PLINTH_IDENTITY_DATABASE_URL`

### Schema parity

Postgres schema mirrors SQLite. Type mappings:
- `TEXT` → `TEXT`
- `INTEGER` → `BIGINT` (where it might overflow `INTEGER` on Postgres)
- `TIMESTAMP` → `TIMESTAMPTZ`
- `JSON-as-TEXT` columns stay `TEXT` (no migration to `JSONB` in v0.4 to keep adapters simple)

### Connection management

Use `asyncpg` directly (no SQLAlchemy in v0.4 — keep deps minimal). Pool size configurable via:
- `PLINTH_DB_POOL_MIN_SIZE=5`
- `PLINTH_DB_POOL_MAX_SIZE=20`

### Migrations

For v0.4: no migration framework. Schema applied idempotently on startup (CREATE TABLE IF NOT EXISTS). Migration tooling in v0.5.

## Workspace Garbage Collection & Retention

Operational hygiene for accumulating versioned data.

### Endpoints (Workspace)

```
POST  /v1/workspaces/{ws_id}/gc                                 → 200 GCResult
GET   /v1/workspaces/{ws_id}/retention                          → 200 RetentionPolicy
PUT   /v1/workspaces/{ws_id}/retention   body: RetentionPolicy  → 200 RetentionPolicy
POST  /v1/admin/gc                                              → 200 GCResult  (sweep all workspaces with policies)
```

### Models

```python
class RetentionPolicy(BaseModel):
    workspace_id: str
    keep_versions: int | None = None      # keep last N versions per key/path
    keep_days: int | None = None          # keep versions newer than N days
    keep_snapshots: int | None = None     # keep last N snapshots
    delete_unreferenced_blobs: bool = True
    updated_at: datetime

class GCResult(BaseModel):
    workspace_id: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    kv_versions_deleted: int
    file_versions_deleted: int
    blob_files_deleted: int
    snapshots_deleted: int
    branches_deleted: int
    bytes_freed: int
```

### Behavior

- GC respects: `keep_versions`, `keep_days`, `keep_snapshots`. The most permissive of the active rules wins per row.
- Versions referenced by any non-deleted snapshot are preserved.
- Blob files (`blobs/<sha256>`) are deleted if no `file_entries` row references them.
- GC is concurrent-safe: uses a lightweight per-workspace advisory lock.
- `POST /v1/admin/gc` requires the calling token to have `tenant:*:admin` scope (or `*`).

## OTLP Event Stream

The Gateway emits semantic events as **OpenTelemetry Logs** (OTLP/HTTP) — replacing audit-log polling with a real observability pipeline.

### Configuration

```
PLINTH_OTLP_ENABLED=true|false                # default false (back-compat)
PLINTH_OTLP_ENDPOINT=http://localhost:4318    # OTLP/HTTP collector
PLINTH_OTLP_SERVICE_NAME=plinth-gateway
PLINTH_OTLP_BATCH_SIZE=64
PLINTH_OTLP_FLUSH_INTERVAL_SECONDS=2.0
PLINTH_OTLP_HEADERS_JSON='{}'                  # e.g. {"Authorization": "Bearer x"}
```

### Event mapping

Every gateway audit_event is also emitted to OTLP as a Log with attributes mirroring `specs/schemas/event.schema.json`. Existing `audit_events` table writes continue (back-compat).

### New endpoints (Gateway)

```
GET  /v1/observability/status        → { "otlp_enabled": bool, "otlp_endpoint": str | null,
                                          "events_emitted": int, "last_emit_at": datetime | null,
                                          "flush_errors": int }
POST /v1/observability/flush         → flushes the buffer immediately (admin-only)
```

### Dashboard additions

`/api/overview` gains a new section:
```json
"observability": {
  "otlp_enabled": true,
  "events_emitted_5min": 142,
  "errors_5min": 0
}
```

The dashboard adds a **time-series graph** (vanilla JS Canvas, no chart lib) of `tool_calls_per_minute` over the last 60 minutes, computed from `audit_events`.

## RS256 Capability Tokens (Identity)

The Identity service grows up: HS256 stays available for back-compat, but production should use RS256 with key rotation.

### Configuration

```
PLINTH_IDENTITY_JWT_ALG=HS256|RS256          # default HS256 for v0.4 back-compat
PLINTH_IDENTITY_KEY_ROTATION_DAYS=30          # rotate signing key every N days
PLINTH_IDENTITY_KEYS_DIR=$DATA_DIR/identity-keys/
```

When `JWT_ALG=RS256`:
- On startup, Identity loads or generates an RSA-2048 private/public key pair stored in `$KEYS_DIR/`.
- Each key has a `kid` (key ID), `created_at`, `expires_at`.
- Tokens issued with the **active** key (most recent unexpired key).
- The `/v1/.well-known/jwks.json` endpoint returns the **last 3** non-expired public keys (so tokens issued with retiring keys still verify until they themselves expire).

### Schema (Identity, additive)

```sql
CREATE TABLE IF NOT EXISTS signing_keys (
  kid TEXT PRIMARY KEY,
  alg TEXT NOT NULL,
  public_key_pem TEXT NOT NULL,
  private_key_pem_encrypted TEXT NOT NULL,    -- AES-GCM, key from PLINTH_IDENTITY_KEYS_ENCRYPTION_KEY
  created_at TIMESTAMP NOT NULL,
  rotated_in_at TIMESTAMP,                    -- when this key became active
  expires_at TIMESTAMP NOT NULL,
  active INTEGER NOT NULL DEFAULT 0
);
```

### New endpoints (Identity)

```
GET  /v1/keys                                → list signing keys (public_pem only, no private material)
POST /v1/keys/rotate                         → force a rotation (admin scope)
DELETE /v1/keys/{kid}                        → expire a key immediately (admin scope)
```

### Verifier changes (Workspace + Gateway + SDKs)

When `verify_local` mode is on with RS256:
- The verifier fetches `$IDENTITY_URL/v1/.well-known/jwks.json` on startup
- Caches keys for 5 minutes; refreshes on token-with-unknown-kid
- Verifies tokens using the matching `kid`'s public key

### SDK Python additions

```python
keys = client.identity.list_keys()             # → list[SigningKey]
client.identity.rotate_key()                   # → SigningKey (the new active)
client.identity.expire_key(kid)
```

### TypeScript SDK additions

Mirror the Python SDK `identity.listKeys()`, `identity.rotateKey()`, `identity.expireKey()`.

## Slack + Linear OAuth Providers

Generic OAuth code from v0.3 already supports new providers; v0.4 adds the configuration and two new MCP servers.

### Configuration (Gateway)

```
PLINTH_OAUTH_SLACK_CLIENT_ID=...
PLINTH_OAUTH_SLACK_CLIENT_SECRET=...
PLINTH_OAUTH_SLACK_SCOPES="channels:read,chat:write,users:read"

PLINTH_OAUTH_LINEAR_CLIENT_ID=...
PLINTH_OAUTH_LINEAR_CLIENT_SECRET=...
PLINTH_OAUTH_LINEAR_SCOPES="read,write"
```

### Slack MCP server (port 7427)

| tool_id | purpose |
|---------|---------|
| `slack.list_channels` | List public + private (where authorized) channels |
| `slack.post_message` | Post message to a channel |
| `slack.list_messages` | Read recent messages in a channel |
| `slack.get_user` | User profile |

### Linear MCP server (port 7428)

| tool_id | purpose |
|---------|---------|
| `linear.list_issues` | List issues (filterable by team / assignee / state) |
| `linear.get_issue` | Issue details |
| `linear.create_issue` | Create issue |
| `linear.update_issue` | Update title / description / state / labels |
| `linear.comment_on_issue` | Add comment |

Both servers behave identically to the GitHub MCP server: read OAuth bearer from forwarded `Authorization` header, return Plynf-shaped error envelopes.

## Stack additions (v0.4)

- `asyncpg>=0.29` (Postgres driver)
- `opentelemetry-api>=1.25`, `opentelemetry-sdk>=1.25`, `opentelemetry-exporter-otlp-proto-http>=1.25`
- `cryptography>=42.0` (already present from v0.3) — used for RS256 keypair generation

---

# v0.5 Additions

The v0.5 release focuses on reliability and coordination depth: real schema migrations, durable workflow execution, workflow transactions with compensating actions, typed channels with dead-letter queue, and stress benchmarks with load-shedding.

## Migration Framework

Each service owns a `migrations/` directory with versioned SQL files (`0001_initial.sql`, `0002_add_tenants.sql`, ...). A migration runner is invoked on startup (idempotent) or via CLI.

### File format

```
services/<name>/migrations/
├── 0001_initial.sql          # baseline schema (matches v0.1)
├── 0002_add_tenants.sql      # v0.3 multi-tenancy
├── 0003_add_channels.sql     # v0.2 channels
├── 0003_add_workflows.sql    # v0.2 workflows
├── 0004_add_retention.sql    # v0.4 retention policies
└── README.md                 # explains the framework
```

Each `.sql` file is a forward migration. Down/rollback is captured in a sibling `<id>_rollback.sql` (optional, recommended for risky changes).

### Tracking table

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  id TEXT PRIMARY KEY,                    -- "0001_initial"
  applied_at TIMESTAMP NOT NULL,
  duration_ms INTEGER NOT NULL,
  checksum TEXT NOT NULL                  -- sha256 of the SQL file content
);
```

### Behavior

- On startup: scan `migrations/`, sort by ID, apply each unseen one inside a transaction.
- Lock during application (advisory lock per service for Postgres; file lock for SQLite).
- Checksum-mismatch on already-applied migration = startup error (someone changed history).
- Migrations must be idempotent where possible; for irreversible ones, document in `_rollback.sql`.

### CLI

```
$ python -m plinth_workspace migrate                # apply pending
$ python -m plinth_workspace migrate --status       # show applied + pending
$ python -m plinth_workspace migrate --to 0003      # apply up to (or rollback past) given ID
$ python -m plinth_workspace migrate --create "add foo"   # scaffold new migration file
```

### Endpoints (per service)

```
GET /v1/admin/migrations                            → 200 { applied: [...], pending: [...], current: "0004" }
POST /v1/admin/migrations/apply                     → 201 { applied: [...], skipped: [...] }
```

Both require admin scope.

## Durable Workflow Executor

The v0.2 workflow primitive stored state but ran the agent in-process. v0.5 introduces a **worker pool** that pulls pending steps from the workspace and executes them with lease semantics — so an agent can crash, the worker keeps running, and another worker takes over if the worker itself dies.

### Schema (Workspace, additive)

```sql
CREATE TABLE IF NOT EXISTS workflow_step_leases (
  step_id TEXT PRIMARY KEY,
  worker_id TEXT NOT NULL,
  acquired_at TIMESTAMP NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  heartbeat_at TIMESTAMP NOT NULL,
  status TEXT NOT NULL DEFAULT 'running'  -- 'running' | 'released' | 'expired'
);
CREATE INDEX IF NOT EXISTS idx_leases_expiry ON workflow_step_leases(expires_at);

CREATE TABLE IF NOT EXISTS workers (
  id TEXT PRIMARY KEY,                    -- "worker_<ulid>"
  hostname TEXT,
  pid INTEGER,
  started_at TIMESTAMP NOT NULL,
  last_heartbeat_at TIMESTAMP NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'   -- 'active' | 'draining' | 'gone'
);
```

### Endpoints (Workspace)

```
POST /v1/workspaces/{ws_id}/workflows/{wf_id}/steps/{step_id}/lease    body: {worker_id, ttl_seconds}  → 200 Lease | 409
POST /v1/workspaces/{ws_id}/workflows/{wf_id}/steps/{step_id}/heartbeat body: {worker_id}  → 200
POST /v1/workspaces/{ws_id}/workflows/{wf_id}/steps/{step_id}/release  body: {worker_id, status}  → 204

POST /v1/workers/register                           body: WorkerRegistration  → 201 Worker
POST /v1/workers/{worker_id}/heartbeat                                       → 200
POST /v1/workers/{worker_id}/drain                                           → 204
GET  /v1/workers                                    ?status=                 → list[Worker]

GET  /v1/workspaces/{ws_id}/workflows/{wf_id}/pending  → list[WorkflowStep]   (steps with status=pending)
GET  /v1/workspaces/{ws_id}/workflows/{wf_id}/expired  → list[Lease]          (leases past expiry, can be reclaimed)
```

### Models

```python
class Lease(BaseModel):
    step_id: str
    worker_id: str
    acquired_at: datetime
    expires_at: datetime
    heartbeat_at: datetime
    status: Literal["running", "released", "expired"]

class WorkerRegistration(BaseModel):
    hostname: str | None = None
    pid: int | None = None

class Worker(BaseModel):
    id: str
    hostname: str | None
    pid: int | None
    started_at: datetime
    last_heartbeat_at: datetime
    status: Literal["active", "draining", "gone"]
```

### Worker process

A new entrypoint:

```bash
python -m plinth_workflow_worker --workspace-url ... --gateway-url ... --identity-url ... \
                                 --concurrency 4 --lease-ttl 60 --heartbeat-interval 15
```

The worker:
1. Registers with the workspace (`/v1/workers/register`)
2. Loops: `poll_pending` → `lease` → `execute` (call agent functions registered via decorator) → `release(status=completed|failed)` → snapshot
3. Heartbeats every 15s
4. On expired leases by other workers, re-poll and try to lease (race-safe)
5. On graceful shutdown: drain (`POST /v1/workers/{id}/drain`)

A separate **lease reaper** runs in the workspace service: every 30s, sweep `workflow_step_leases WHERE expires_at < NOW() AND status = 'running'`, mark them expired so other workers can pick them up.

### SDK (Python) addition

```python
@client.workflow_handler("research-pipeline", step="search")
def handle_search_step(ctx, step):
    """Decorator registers this function as a step handler."""
    topic = step.input["topic"]
    # ... do work via ctx.tools, ctx.workspace ...
    return {"sources": [...]}

# Start a worker
client.run_workflow_worker(concurrency=4)  # blocks; spawns workers
```

The SDK serializes registered handlers and their step names; workers fetch this map at startup.

## Workflow Transactions with Compensating Actions

A **transaction** is a sequence of tool calls grouped as a unit. Each call may register a compensation. If the transaction fails partway, executed compensations roll back side effects in reverse order.

### Endpoints (Gateway)

```
POST /v1/transactions                       body: TransactionCreate  → 201 Transaction
POST /v1/transactions/{tx_id}/calls         body: TransactionCallAdd → 201 TransactionCall
POST /v1/transactions/{tx_id}/commit                                 → 200 TransactionResult  (executes all calls, returns outputs)
POST /v1/transactions/{tx_id}/rollback                               → 200 TransactionResult  (executes compensations only)
GET  /v1/transactions/{tx_id}                                        → 200 Transaction
```

### Models

```python
class TransactionCreate(BaseModel):
    workspace_id: str | None = None
    agent_id: str | None = None
    metadata: dict = {}

class TransactionCallAdd(BaseModel):
    tool_id: str
    arguments: dict
    compensation: CompensationSpec | None = None

class CompensationSpec(BaseModel):
    """Defines how to undo a successful call."""
    tool_id: str                # may be different from the forward tool
    arguments_template: dict    # may reference the forward call's result via `{result.field}` placeholders

class Transaction(BaseModel):
    id: str                     # tx_<ulid>
    status: Literal["pending", "committing", "committed", "compensating", "rolled_back", "failed"]
    workspace_id: str | None
    agent_id: str | None
    calls: list[TransactionCall]
    created_at: datetime
    committed_at: datetime | None
    rolled_back_at: datetime | None

class TransactionCall(BaseModel):
    id: str                     # txc_<ulid>
    tx_id: str
    seq: int                    # order in transaction
    tool_id: str
    arguments: dict
    compensation: CompensationSpec | None
    status: Literal["pending", "running", "committed", "compensating", "compensated", "failed"]
    result: Any | None
    error: str | None
    
class TransactionResult(BaseModel):
    tx_id: str
    status: str
    calls: list[TransactionCall]
    compensations_run: int
```

### Schema (Gateway)

```sql
CREATE TABLE IF NOT EXISTS transactions (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  agent_id TEXT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL DEFAULT 'pending',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TIMESTAMP NOT NULL,
  committed_at TIMESTAMP,
  rolled_back_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transaction_calls (
  id TEXT PRIMARY KEY,
  tx_id TEXT NOT NULL REFERENCES transactions(id),
  seq INTEGER NOT NULL,
  tool_id TEXT NOT NULL,
  arguments TEXT NOT NULL,
  compensation_spec TEXT,            -- JSON
  status TEXT NOT NULL DEFAULT 'pending',
  result TEXT,
  error TEXT,
  invoked_at TIMESTAMP,
  finished_at TIMESTAMP,
  UNIQUE (tx_id, seq)
);
```

### Commit semantics

`POST /v1/transactions/{tx_id}/commit`:
1. Set status = `committing`
2. For each call in seq order:
   - Mark call `running`, invoke tool via existing `/v1/invoke` machinery (audit, cache, OAuth all apply)
   - On success: store result, mark `committed`
   - On failure: mark `failed`, **trigger compensation cascade**
3. Compensation cascade: iterate already-`committed` calls in **reverse** order. For each, if `compensation_spec` is set, render `arguments_template` substituting `{result.<field>}` from the forward result, invoke that tool. Mark call `compensated`.
4. Return TransactionResult with all call outcomes.

### SDK

```python
tx = client.gateway.transaction(workspace_id=ws.id, agent_id="my-agent")
tx.add(
    "github.create_issue",
    {"repo": "owner/name", "title": "..."},
    compensation=("github.update_issue", {"repo": "owner/name", "issue_number": "{result.number}", "state": "closed"}),
)
tx.add(
    "slack.post_message",
    {"channel": "C123", "text": "Issue created: {result.html_url}"},  # references previous call's result
    compensation=None,  # nothing to undo for posting a Slack message
)
result = tx.commit()  # or tx.rollback() to undo without committing
print(result.status, result.calls)
```

## Typed Channels + Dead-Letter Queue

Channels grow an optional schema; failed-validation messages route to a dead-letter sub-channel.

### Endpoints (Workspace)

```
POST   /v1/workspaces/{ws_id}/channels/{name:path}/schema   body: {schema: dict}  → 200 ChannelSchema
DELETE /v1/workspaces/{ws_id}/channels/{name:path}/schema                         → 204
GET    /v1/workspaces/{ws_id}/channels/{name:path}/deadletter                     → list[ChannelMessage]
POST   /v1/workspaces/{ws_id}/channels/{name:path}/deadletter/{msg_id}/replay     → 200 (re-validates and re-sends)
```

### Send-time behavior

When a channel has a schema attached:
- `POST .../send` validates the payload against the JSON Schema (`jsonschema` library).
- On validation failure: the message is delivered to a hidden `<channel>.deadletter` channel **and** the original send returns 422 with `{"code": "SCHEMA_VIOLATION", "details": {"errors": [...], "deadletter_msg_id": "..."}}`.

### DLQ retrieval + replay

DLQ channel is internal; only accessible via `GET /v1/.../deadletter` (paginated).

`POST .../deadletter/{msg_id}/replay`:
- Fetch the message from DLQ
- Re-validate against the (now possibly updated) schema
- If valid: send to main channel, delete from DLQ
- If still invalid: 422 with new errors, message stays in DLQ

### Schema evolution

If a schema is updated and previously-valid messages would now fail: existing messages remain in the channel as-is (they were valid at send time). Receiver opt-in to re-validate on read via `?revalidate=true`.

### Models

```python
class ChannelSchema(BaseModel):
    workspace_id: str
    channel_name: str
    schema_json: dict          # JSON Schema document
    updated_at: datetime
    version: int               # increments on each PUT
```

### SDK

```python
ws.channels.set_schema("research-out", {
    "type": "object",
    "required": ["topic", "sources"],
    "properties": {
        "topic": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
})

# This raises plinth.SchemaViolation if payload is invalid
try:
    ws.channels.send("research-out", {"topic": "x"})  # missing 'sources'
except SchemaViolation as e:
    print(e.deadletter_msg_id)

# Inspect DLQ
dlq = ws.channels.deadletter("research-out")
ws.channels.replay(dlq[0].id)   # try again with current schema
```

### Dashboard

Add a "Dead Letters" panel listing channels with non-empty DLQs and counts.

## Stress Benchmarks + Load-Shedding

A new `benchmarks/` directory + middleware in workspace + gateway that returns 503 when overloaded instead of slowly grinding to a halt.

### Benchmark suite

```
benchmarks/
├── README.md
├── pyproject.toml                # depends on plinth, locust or k6 wrapper
├── workspace_kv.py               # PUT/GET KV at varying RPS
├── workspace_files.py
├── gateway_invoke.py             # /v1/invoke with cached + cold-cache
├── gateway_invoke_with_oauth.py
├── identity_token_issue.py
├── results/                      # JSON output per run
└── compare.py                    # produce a markdown table from a results/ JSON
```

Each benchmark runs against a configured base URL, ramps RPS from 10→1000, captures p50/p95/p99/error_rate, and writes JSON to `results/`.

### Load-shedding middleware

In Workspace + Gateway, configurable middleware:

```python
PLINTH_LOAD_SHED_ENABLED=true|false               # default false (back-compat)
PLINTH_LOAD_SHED_MAX_INFLIGHT=200                  # per-process inflight limit
PLINTH_LOAD_SHED_MAX_QUEUE=1000                    # pending request queue
PLINTH_LOAD_SHED_RETRY_AFTER_SECONDS=1
```

When `inflight + queued > max`: return 503 with `Retry-After`. Gauge `plinth.shed.requests_total` exposed via existing audit/OTLP path.

### `make bench` target

```bash
make bench                                                # runs the standard suite, writes to benchmarks/results/
make bench-compare BASELINE=<run-id>                      # compares latest run vs baseline
```

### Documented numbers in README

After v0.5 ships, README gains a "Performance" section showing measured numbers for a fixed dev-machine target.

## Worker process — `plinth-workflow-worker` (NEW)

A new top-level Python module + CLI used by Deliverable B (Durable Workflow Executor).

```
worker/
├── pyproject.toml
├── README.md
├── src/plinth_workflow_worker/
│   ├── __init__.py             # __version__ = "0.5.0"
│   ├── __main__.py             # CLI entrypoint
│   ├── worker.py               # main loop, handler dispatch, leases, heartbeats
│   ├── settings.py
│   └── logging_config.py
└── tests/
```

### CLI

```bash
plinth-workflow-worker \
  --workspace-url http://localhost:7421 \
  --gateway-url http://localhost:7422 \
  --identity-url http://localhost:7425 \
  --api-key "..." \
  --concurrency 4 \
  --lease-ttl 60 \
  --heartbeat-interval 15 \
  --handlers-module myapp.handlers
```

`--handlers-module` is the importable Python module that registered handlers via `@client.workflow_handler(...)`. Worker imports it on startup, builds the dispatch table, then loops.

## Stack additions (v0.5)

- `jsonschema>=4.20` — typed channels validation
- `apscheduler>=3.10` — worker heartbeat scheduling (or asyncio-only, agent's choice)
- (benchmarks) `locust>=2.20` or `httpx[http2]` + `asyncio` — minimal load tooling

## Backwards compatibility

- All v0.1–v0.4 endpoints unchanged
- Migration framework auto-applies on startup; existing CREATE-IF-NOT-EXISTS schema is captured as `0001_initial.sql` so existing databases just record one row in `schema_migrations` and proceed
- Durable executor is opt-in (no workers running = workflows still work in-process as v0.2)
- Transactions are a NEW endpoint family — no impact on existing `/v1/invoke` callers
- Typed channels: schema is optional. Untyped channels behave exactly as v0.2.
- Load-shedding is opt-in (`PLINTH_LOAD_SHED_ENABLED=false` default).

---

# v0.6 Additions

The v0.6 release focuses on **distribution** (multi-node coordination) and **polish** (rollback, visualization, generic locks, schema migration UX).

## Federated Revocation (Identity service)

When Identity runs as multiple replicas behind a load balancer, a `revoke` on one replica must propagate to all others. v0.6 introduces a polling-based propagation mechanism (simple, no extra infra).

### New endpoints (Identity)

```
GET  /v1/revocations                 ?since=<unix_ts>&limit=<int>  → 200 RevocationList
GET  /v1/revocations/stats                                          → 200 { total: int, since_24h: int, since_1h: int }
```

### Models

```python
class RevocationEntry(BaseModel):
    jti: str
    revoked_at: datetime
    agent_id: str
    tenant_id: str

class RevocationList(BaseModel):
    revocations: list[RevocationEntry]
    next_since: int          # cursor — caller passes this on next poll
    has_more: bool
```

### Behavior

- `since` is a unix timestamp; only revocations with `revoked_at > since` are returned.
- Default `limit=1000`, max `2000`.
- Caller maintains its own cursor; idempotent re-polls return same data.

### Workspace + Gateway changes

Both services gain a **revocation cache** (in-memory `set[str]` of revoked JTIs) refreshed every 60s by polling Identity.

```python
class Settings(BaseSettings):
    # ... existing ...
    revocation_poll_url: str = ""              # Identity URL, e.g. http://identity:7425
    revocation_poll_interval_seconds: int = 60
    revocation_poll_enabled: bool = True
```

When verifying a JWT locally, after signature/expiry checks, the auth middleware also checks the local revocation cache. If `jti in cache` → reject with `TOKEN_REVOKED`.

If polling fails (Identity unreachable): cache stays as-is; log warning. Revocations newly issued by Identity won't propagate until polling resumes — documented as known limitation.

## Postgres Advisory Locks (Migration runner)

Replaces the `fcntl.flock` lock with `pg_advisory_lock(<service_id_hash>)` when running on Postgres. Allows multiple replicas to start simultaneously without race.

Implementation: `MigrationRunner.acquire_lock()` checks the configured DB driver; chooses `flock` for SQLite, `pg_advisory_lock` for Postgres. Lock identifier: `hashtext('plinth_migrations_' || service_name)::int`.

No new endpoints. Behavior is invisible to callers but verified via concurrent-replica tests.

## Migration Rollback

Apply `<id>_rollback.sql` files in reverse order to roll back to a target migration.

### CLI

```bash
python -m plinth_workspace migrate --rollback-to 0003_workflows
python -m plinth_workspace migrate --rollback-to 0003_workflows --dry-run
```

### Endpoint

```
POST /v1/admin/migrations/rollback   body: { to: "0003_workflows", dry_run: false }   → 200 RollbackResult
```

### Behavior

1. Identify all applied migrations with id > target → these need rollback.
2. For each, in **reverse application order**: read `<id>_rollback.sql`. If missing → fail with `MIGRATION_ROLLBACK_MISSING` (don't continue).
3. Execute each rollback in its own transaction. Update `schema_migrations` (delete row).
4. Verify checksums of rollback files match recorded `rollback_checksum` if previously stored.
5. Return list of rolled-back migrations.

### Models

```python
class RollbackResult(BaseModel):
    target: str
    rolled_back: list[str]
    skipped: list[str]
    failed: str | None = None
    error_message: str | None = None
```

### Schema additions

Add to `schema_migrations`: `rollback_checksum TEXT` column (nullable; populated on apply if rollback file exists).

## Generic Lock/Lease Primitives

Locks for any named resource — not just workflow steps. Useful for Agent-A-vs-Agent-B race protection on KV/files/external resources.

### Endpoints (Workspace)

```
POST /v1/workspaces/{ws_id}/locks/{name:path}/acquire   body: { holder, ttl_seconds, wait_ms? }  → 200 Lock | 409
POST /v1/workspaces/{ws_id}/locks/{name:path}/heartbeat body: { holder }                          → 200 Lock
POST /v1/workspaces/{ws_id}/locks/{name:path}/release   body: { holder }                          → 204
GET  /v1/workspaces/{ws_id}/locks                                                                  → 200 list[Lock]
GET  /v1/workspaces/{ws_id}/locks/{name:path}                                                      → 200 Lock | 404
```

### Models

```python
class Lock(BaseModel):
    name: str
    workspace_id: str
    holder: str
    acquired_at: datetime
    expires_at: datetime
    heartbeat_at: datetime
    waiters: int = 0          # info only

class LockAcquireBody(BaseModel):
    holder: str               # caller-chosen identifier
    ttl_seconds: int = 60
    wait_ms: int = 0          # 0 = fail-fast; >0 = poll-wait up to wait_ms

class LockHeartbeatBody(BaseModel):
    holder: str               # must match current holder

class LockReleaseBody(BaseModel):
    holder: str
```

### Schema (Workspace)

```sql
CREATE TABLE IF NOT EXISTS resource_locks (
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  holder TEXT NOT NULL,
  acquired_at TIMESTAMP NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  heartbeat_at TIMESTAMP NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_locks_expiry ON resource_locks(expires_at);
```

### Behavior

- **Acquire**: race-safe upsert. If lock exists and not expired → 409 Conflict (with `Retry-After` if `wait_ms=0`); if `wait_ms>0` → poll up to that duration.
- **Heartbeat**: extends expires_at; only succeeds if `holder` matches.
- **Release**: deletes row; only if `holder` matches; idempotent (no-error if already released).
- **Lease reaper**: existing reaper extended to also sweep expired resource locks.

### SDK

```python
# Context manager
with ws.locks.acquire("kv:sources/index", holder="agent-A", ttl_seconds=30):
    # critical section — no other holder can acquire this lock
    ws.kv.set("sources/index", new_value)
# automatic release on exit

# Or low-level
lock = ws.locks.acquire("name", holder="...", ttl_seconds=60)
try:
    ...
finally:
    ws.locks.release("name", holder="...")
```

## Workflow Visualization (Dashboard)

Dashboard gains a per-workflow visual route. Reads existing API endpoints — no service changes needed.

### Routes (added to existing dashboard SPA)

```
/workflows                                       → list of all workflows across all workspaces (default tenant)
/workflows/{wf_id}?ws=<ws_id>                    → graph view of a single workflow
```

### `/api/workflows/overview` (new endpoint in dashboard)

Aggregates from workspace `/v1/workspaces` → `/v1/workspaces/{ws_id}/workflows` for dashboard's tenant.

### Graph rendering (frontend)

- Pure SVG/HTML — no D3/cytoscape — keep dependency-free
- Steps as nodes, in `steps_manifest` order, left-to-right
- Each node colored by status: pending=grey, running=blue, completed=green, failed=red, cancelled=orange
- Node shows: step name, attempt count, lease holder (if running), duration if completed
- Auto-refresh every 5s (matches existing dashboard pattern)
- Click node → modal with step details (input, output, error, snapshot_id)

### Tests

- /workflows route renders SPA shell
- /api/workflows/overview aggregates correctly
- Empty (no workflows) state
- Dashboard tests for the new aggregator endpoint

## Channel Schema Migration Helpers

Helpers for evolving channel schemas without losing in-flight data.

### Endpoints (Workspace, additive)

```
POST /v1/workspaces/{ws_id}/channels/{name:path}/schema/check   body: { schema, scope: "main"|"deadletter"|"both", limit: int = 1000 }
                                                                 → 200 SchemaCheckResult
POST /v1/workspaces/{ws_id}/channels/{name:path}/deadletter/replay-all
                                                                 ?dry_run=&max=  → 200 ReplayBatchResult
DELETE /v1/workspaces/{ws_id}/channels/{name:path}/deadletter   ?older_than_seconds=    → 204  (purge old DLQ)
```

### Models

```python
class SchemaCheckResult(BaseModel):
    channel: str
    scope: Literal["main", "deadletter", "both"]
    checked: int
    valid: int
    invalid: int
    sample_failures: list[dict]      # first N validation errors
    
class ReplayBatchResult(BaseModel):
    channel: str
    attempted: int
    succeeded: int
    failed: int
    failures: list[dict]             # message_id + reason for each failure
    dry_run: bool
```

### Behavior

**`check`**: against either main, DLQ, or both, simulate validating each message's payload against the proposed `schema`. Return counts + sample failures. Doesn't mutate.

**`replay-all`**: bulk version of single-message replay. Iterates DLQ, attempts replay (re-validates against current schema). Returns per-message outcomes. With `dry_run=true`: doesn't actually move messages.

**`purge`**: delete DLQ messages older than `older_than_seconds`. Returns count.

### SDK

```python
result = ws.channels.check_schema("research-out", new_schema, scope="both")
# preview compatibility before committing

batch = ws.channels.replay_all_dlq("research-out", dry_run=True)
# see what would happen

batch = ws.channels.replay_all_dlq("research-out", max=100)
# actual replay

ws.channels.purge_dlq("research-out", older_than_seconds=86400)
# hygiene
```

## Stack additions (v0.6)

- No new runtime dependencies. All v0.6 work uses existing stack.

## Backwards compatibility

- Federated revocation: opt-in via `PLINTH_REVOCATION_POLL_URL`. Without it, services behave exactly as v0.5.
- Postgres advisory locks: only active when running against Postgres; SQLite path unchanged.
- Migration rollback: only available via explicit CLI/endpoint; never auto-runs.
- Generic locks: new endpoints, no impact on existing.
- Channel schema migration helpers: new endpoints; existing single-message replay unchanged.
- Workflow viz: dashboard-only; backend unchanged.

All v0.1–v0.5 demos must produce unchanged output.

---

# v1.0 Additions — General Availability

The v1.0 release is the GA milestone: **stable API guarantees, production-ready ops, multi-region capability, compliance scaffolding, and a unified operator CLI**. It rolls up everything that was on the v0.7–v0.9 trajectory into one coherent ship.

## Per-Tenant Resource Quotas

Each tenant gets an enforceable quota envelope. Quotas live in Identity (the source of tenant truth) and are read by Workspace + Gateway when accepting create/invoke calls.

### Endpoints (Identity)

```
POST   /v1/tenants/{tenant_id}/quotas    body: TenantQuotas         → 200 TenantQuotas
GET    /v1/tenants/{tenant_id}/quotas                                → 200 TenantQuotas
DELETE /v1/tenants/{tenant_id}/quotas                                → 204 (revert to defaults)
GET    /v1/tenants/{tenant_id}/usage                                 → 200 TenantUsage
```

### Models

```python
class TenantQuotas(BaseModel):
    tenant_id: str
    max_workspaces: int = 100
    max_storage_gb: float = 10.0
    max_channels_per_workspace: int = 50
    max_workflows_per_workspace: int = 100
    max_active_tokens: int = 1000
    max_oauth_connections: int = 50
    max_cost_usd_day: float = 100.0
    max_cost_usd_month: float = 2000.0
    max_invocations_per_minute: int = 600
    updated_at: datetime

class TenantUsage(BaseModel):
    tenant_id: str
    workspaces: int
    storage_gb: float
    active_tokens: int
    oauth_connections: int
    cost_usd_day: float
    cost_usd_month: float
    last_invocation_at: datetime | None
```

### Enforcement

Workspace `POST /v1/workspaces` checks `tenant_usage.workspaces < quota.max_workspaces` → else 429 with `QUOTA_EXCEEDED`. Same pattern on channel/workflow create, file write (storage_gb), gateway invoke (rate + cost). Errors carry `details.quota = "max_workspaces"` etc.

## Tenant Admin UI (Dashboard)

Dashboard gains `/tenants` route:
- List tenants (id, name, member count, quota usage bars)
- Create tenant
- Edit quotas (form)
- Delete tenant (with confirm + GDPR-export prompt)
- Per-tenant detail page: members, OAuth connections, recent audit, cost rollup

Endpoints: `/api/tenants` (proxies Identity).

## Channel-Schema-Evolution Wizard (Dashboard)

Visual UI for the v0.6 schema-migration helpers:
- Edit schema in a JSON editor (with validation)
- Click "Check compatibility" → calls `schema/check`, renders pass/fail counts + first 10 errors
- If valid: "Apply schema" button → POSTs to `set_schema`
- "Replay all DLQ" + "Purge older than" buttons (already in v0.6)

## Multi-Region Scaffolding

v1.0 doesn't *run* multi-region by default but provides the scaffolding:

### Region configuration

Each service accepts:
```
PLINTH_REGION_ID=eu-west-1                  # this instance's region
PLINTH_REGION_PEERS=us-east-1,ap-south-1   # comma-separated peer regions
PLINTH_REGION_PEERS_<id>_URL=https://...    # peer URLs
PLINTH_REPLICATION_MODE=primary|replica|standalone
```

### Replication

- Workspace + Identity: log-shipping for SQLite-based deployments (a periodic SQL dump streamed to peers); native streaming replication for Postgres.
- Read-replicas accept GET-only requests; redirect mutating requests to primary with `X-Plynf-Primary-Region` header.

### SDK addition

```python
client = Plynf(
    workspace_url="https://workspace.plinth.example",
    region="eu-west-1",                # SDK can route region-aware
    fallback_regions=["us-east-1"],    # automatic failover on 503/connection
)
```

### Endpoint

```
GET /v1/regions                       → 200 { current: str, peers: [{id, url, status, lag_ms}] }
```

## Unified CLI: `plinth`

A single Python CLI consolidating ops:

```
plinth services start | stop | status | logs <svc>
plinth migrate <svc> --status | --apply | --rollback-to <id>
plinth workflow list | show <wf_id> | cancel <wf_id> | resume <wf_id>
plinth audit --tool <id> --since 1h --workspace <ws>
plinth tenant list | create <id> | quotas <id> | usage <id> | export <id>
plinth health
plinth bench quick | full | compare <a> <b>
plinth completion install
```

Built on Click. Reads `~/.plinth/config.toml` for service URLs + API key (with env-var override). Tab-completion for tenant/workspace/workflow IDs.

Distributed as a separate package `plinth-cli` (top-level `cli/` directory).

## Compliance Scaffolding

### GDPR data export

```
POST /v1/tenants/{tenant_id}/export                   → 202 { export_id }
GET  /v1/tenants/{tenant_id}/exports/{export_id}      → 200 ExportStatus  (status: pending|ready|expired)
GET  /v1/tenants/{tenant_id}/exports/{export_id}/download   → 200 application/zip
```

ZIP contains: workspaces (kv as JSONL, files as raw), audit events JSONL, OAuth connections (token-redacted), tenants/quotas, workflow records.

### GDPR data deletion

```
DELETE /v1/tenants/{tenant_id}/data?confirm=<token>   → 202 DeleteJob
```

Hard-delete cascade: workspaces → kv/files/snapshots/branches/channels/workflows/transactions, audit events filtered by tenant_id, OAuth connections, identity tokens, tenant row itself. Two-phase confirm via opaque token.

### Tamper-evident audit chain

Each `audit_events` row gets a `prev_hash` column. Hash chain: `hash = sha256(prev_hash || event_canonical_json)`. Verification endpoint:

```
GET /v1/audit/verify?since=<ts>     → 200 { verified: bool, broken_at: id | null }
```

### Threat model

`docs/threat-model.md` — STRIDE-based, attacker classes, mitigations, residual risks.

## API v1 Stability Promise

`docs/API_STABILITY.md` codifies:
- All endpoints under `/v1/...` are guaranteed backwards-compatible until v2 ships
- Deprecation policy: 12-month notice via `Deprecation:` and `Sunset:` HTTP headers
- Additive-only changes within v1 (new optional fields, new endpoints, never breaking changes)
- Contract tests (`tests/contract/`) that verify the OpenAPI specs match running services

A deprecation header on a deprecated endpoint:
```
Deprecation: true
Sunset: Wed, 01 May 2027 00:00:00 GMT
Link: <https://docs.plynf.com/api/v2/migration>; rel="alternate"
```

## Production Deployment Artifacts

```
deploy/
├── k8s/
│   ├── workspace.yaml     # Deployment + Service + ConfigMap
│   ├── gateway.yaml
│   ├── identity.yaml
│   ├── dashboard.yaml
│   ├── mock-mcp.yaml
│   └── kustomization.yaml
├── helm/plinth/
│   ├── Chart.yaml
│   ├── values.yaml         # tenant config, region, replicas, etc.
│   └── templates/
└── terraform/aws-example/
    ├── main.tf              # EKS + RDS Postgres + S3 + IAM
    ├── variables.tf
    └── outputs.tf
```

Plus `.github/workflows/release.yml` for image build + push to GHCR on tag.

## Comprehensive Metrics

### Prometheus exporter

Each service exposes `GET /metrics` (Prometheus format). Standard metrics:
- `plinth_http_requests_total{service, method, status}`
- `plinth_http_request_duration_seconds{service, method}`
- `plinth_tool_invocations_total{tool_id, tenant_id, cached}`
- `plinth_workflow_steps_total{state}`
- `plinth_load_shed_total`
- `plinth_workers_active`

### OTLP attribute consistency

Documented attribute set in `docs/observability.md`. All services emit the same attribute names: `tenant.id`, `agent.id`, `workspace.id`, `tool.id`, `workflow.id`, etc.

### SLOs

`docs/slos.md` — published targets:
- Workspace `GET /v1/workspaces/{id}/kv/{key}`: p99 < 50ms (cached cluster)
- Gateway `POST /v1/invoke` cache-hit: p99 < 30ms
- Identity `POST /v1/tokens/verify`: p99 < 20ms
- Workflow lease acquisition: p95 < 100ms

### Dashboard time-series

24h + 7d rolling time-series graphs for: cost, latency p99, error rate, cache hit ratio, active workers.

## Production-Readiness Checklist

`PRODUCTION_READINESS.md` — operator checklist with ~50 items: backups, monitoring, alerting, runbooks, rollback procedures, disaster recovery, cost limits, security hardening, etc.

## Backwards compatibility (v1.0)

- All v0.1–v0.6 endpoints unchanged
- Per-tenant quotas: existing tenants get default quotas auto-applied; large defaults so existing workloads don't get throttled
- Multi-region: opt-in via env vars; standalone mode is default
- Compliance endpoints are NEW (additive)
- Audit hash chain: column added with `NULL` allowed; legacy rows get `prev_hash=NULL`. Verification only checks rows with hashes.
- Prometheus `/metrics` endpoint: new, additive
- CLI is a separate package — no impact on services

All v0.1–v0.6 demos must produce unchanged output.

---

# v1.1 Additions — Engineering-Debt Sweep + Notion / Google Workspace

v1.1 is purely additive on top of v1.0 GA. API v1 contract is fully preserved. The release lands engineering-debt cleanups (Redis coordination, OTel migration, workflow retries, CI hardening) and two new OAuth providers (Notion + Google Workspace) with their MCP servers.

## OTel — public `logs` API

Migrate from `opentelemetry.sdk._logs` (internal) to the public `opentelemetry.sdk.logs` API in OTel SDK ≥ 1.30. Lift the `<1.30` pin in `services/gateway/pyproject.toml`. Behavior unchanged from caller's perspective.

## Cluster-Aware Coordination — Redis Backend

A new pluggable backend interface for distributed state previously held in single-process memory:

- **Revocation cache** (Identity → polled by Workspace + Gateway)
- **Rate-limit token buckets** (Gateway)
- **Cost-cap rolling windows** (Gateway, per-tenant)
- **Lease coordination** (Workspace, currently fcntl/SQLite-only)

### Settings

```
PLINTH_COORDINATION_BACKEND=memory|redis        # default memory (back-compat)
PLINTH_COORDINATION_REDIS_URL=redis://localhost:6379/0
PLINTH_COORDINATION_KEY_PREFIX=plinth           # multi-tenant cluster sharing
```

### Backend interface

Each service has a `coordination.py` module exposing:

```python
class CoordinationBackend(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def incr(self, key: str, amount: int = 1, ttl_seconds: int | None = None) -> int: ...
    async def add_to_set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...
    async def members(self, key: str) -> set[str]: ...
    async def acquire_lock(self, key: str, holder: str, ttl_seconds: int) -> bool: ...
    async def release_lock(self, key: str, holder: str) -> bool: ...

class MemoryBackend(CoordinationBackend): ...
class RedisBackend(CoordinationBackend): ...
```

When `coordination_backend=memory`: behavior identical to v1.0 (per-process). When `redis`: rate-limits/cost-caps/revocation are cluster-shared.

Behavior:
- Rate-limits: token bucket implemented with Redis Lua script for race-safety
- Cost-caps: sorted-set + ZADD/ZREMRANGEBYSCORE for rolling windows
- Revocation: `revoked_jtis` set + 60s TTL; pub/sub channel for instant-propagation (best-effort)
- Lease coordination: SET NX EX pattern, replaces fcntl lock for multi-replica

### Backwards compatibility

Default is `memory` — v1.0 deployments unchanged. Redis backend opt-in via env var.

## Workflow Retries + Dead-Letter Queue

Each workflow step gains a retry policy; failed steps that exceed `max_attempts` go to a per-workflow DLQ.

### Step model additions

```python
class WorkflowStep(BaseModel):
    # ... existing v0.5 fields ...
    max_attempts: int = 1                # default: no retry (v0.5 behavior)
    retry_policy: Literal["none", "exponential", "fixed"] = "none"
    retry_initial_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 60.0
    retry_jitter: bool = True
    next_retry_at: datetime | None = None  # set when status=failed but attempt < max_attempts
```

Behavior:
- `max_attempts=1`: identical to v1.0
- `max_attempts>1`: on failure, increment `attempt` counter, compute `next_retry_at = now + delay(attempt)` where delay is `initial × 2^(attempt-1)` capped at `max`, optionally with ±25% jitter
- Worker poll-pending excludes steps where `next_retry_at > now`
- After `attempt == max_attempts` failure: route to DLQ

### DLQ for workflows

```sql
CREATE TABLE IF NOT EXISTS workflow_dlq (
  id TEXT PRIMARY KEY,                  -- dlqstep_<ulid>
  step_id TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  step_name TEXT NOT NULL,
  attempts INTEGER NOT NULL,
  last_error TEXT,
  failed_at TIMESTAMP NOT NULL,
  step_snapshot TEXT NOT NULL           -- JSON of the WorkflowStep at failure
);
```

### Endpoints

```
GET  /v1/workspaces/{ws}/workflows/{wf}/dlq                  → list[DLQEntry]
POST /v1/workspaces/{ws}/workflows/{wf}/dlq/{id}/replay      → 200 (re-queues with attempts=0)
DELETE /v1/workspaces/{ws}/workflows/{wf}/dlq/{id}            → 204
```

## Migration Rollback Files

Every existing migration gets a paired `<id>_rollback.sql`. Schema_migrations table tracks rollback_checksum for tamper detection.

## Lease Reaper Jitter

Lease reaper pass adds ±25% jitter to interval to prevent thundering-herd when multiple workspace replicas wake at the same wall-clock second.

## Notion MCP Server (NEW — port 7429)

Provides agent access to Notion via OAuth.

Tools:
- `notion.search` — search across workspace pages/databases
- `notion.get_page` — fetch a page by ID with content
- `notion.create_page` — create a new page (in a database or as a child)
- `notion.update_page` — update properties or content
- `notion.append_block` — append blocks to existing page
- `notion.list_databases` — list accessible databases
- `notion.query_database` — query a database with filter/sort

OAuth: standard Notion OAuth2 flow. Scopes documented in operator guide.

## Google Workspace MCP Server (NEW — port 7430)

Provides agent access to Drive, Docs, Sheets, Calendar, Gmail (read-only by default).

Tools (initial set, read-only + safe writes):
- `google.drive_search` — list files matching query
- `google.drive_read` — read a file's content (Doc/Sheet/PDF)
- `google.docs_create` — create a new Doc with content
- `google.docs_append` — append content to existing Doc
- `google.sheets_read` — read a range from a Sheet
- `google.sheets_append_row` — append a row to a Sheet
- `google.calendar_list_events` — read upcoming events
- `google.gmail_list_messages` — read inbox messages (`labelIds=INBOX`)

OAuth: Google OAuth2 flow with incremental authorization scopes.

## CI Hardening

`.github/workflows/ci.yml` extended:
- Run ALL test suites (was: workspace + gateway + sdk + mock-mcp; v1.1: also identity + dashboard + github-mcp + slack-mcp + linear-mcp + worker + cli + benchmarks + contract)
- Postgres service container, run skipped Postgres tests with `PLINTH_TEST_POSTGRES_URL` set
- New: CodeQL workflow for security scanning
- New: Dependabot config for npm + pip + GitHub Actions
- New: Issue templates (bug, feature, question)
- New: PR template

## Real Benchmark Numbers

`benchmarks/results/baseline-v1.1.json` populated with actual measurements against `make serve` stack. README "Performance" table updated with real numbers.

## Stack additions (v1.1)

- `redis>=5.0` — coordination backend
- `aioredis` (or `redis.asyncio` from `redis>=5.0`) — async client
- `opentelemetry-sdk>=1.30` — public logs API (pin lifted)
- `opentelemetry-exporter-otlp-proto-http>=1.30`

## Backwards compatibility (v1.1)

- All v1.0 endpoints unchanged
- `coordination_backend=memory` default — existing deployments behave identically
- Workflow retries opt-in via `max_attempts>1`
- DLQ endpoints additive
- Notion + Google Workspace are new MCP servers (separate ports)
- API v1 contract preserved; deprecation policy unchanged

All v0.1–v1.0 demos produce unchanged output.

# v1.2 Additions — LLM Layer (Python SDK)

The Python SDK gains a first-class `client.llm` namespace so agents
no longer need to bring their own LLM library. v1.2 ships:

- A provider abstraction (`plinth.llm.LLMProvider` protocol) covering
  sync + async `complete` / `stream` and a `estimate_cost_usd` helper.
- Built-in providers for Anthropic and OpenAI behind opt-in pip extras
  (`pip install 'plinth[anthropic]'`, `pip install 'plinth[openai]'`),
  plus a `MockProvider` that ships unconditionally for tests/demos.
- Retry-with-back-off on 429 (honours `Retry-After`) and 5xx; no retry
  on other 4xx.
- Cost-tracking integrated with the gateway audit log via a new
  `POST /v1/audit/record-llm` endpoint.

## SDK surface

```python
from plinth import Plynf, LLMMessage

client = Plynf(api_key="local-dev")

# Anthropic auto-configures from ANTHROPIC_API_KEY when no provider is
# explicitly set; otherwise:
client.llm.use_provider("anthropic", api_key="sk-ant-...")
# or
client.llm.use_provider("openai", api_key="sk-...")
# or for tests / offline demos:
client.llm.use_provider("mock", responses=["First response", "Second"])

# Synchronous completion
response = client.llm.complete(
    model="claude-sonnet-4-5",
    messages=[{"role": "user", "content": "Hello"}],
    max_tokens=1024,
    temperature=0.7,
    workspace_id=ws.id,        # for audit attribution
    agent_id="my-agent",
)
# response.content : str
# response.input_tokens / output_tokens / cost_usd
# response.duration_ms / model / finish_reason
# response.audit_id (set when audit recording succeeds)
# response.raw (provider-native dict)

# Streaming
for chunk in client.llm.stream(model="...", messages=[...]):
    print(chunk.delta, end="", flush=True)

# Async equivalents
response = await client.llm.acomplete(model="...", messages=[...])
async for chunk in client.llm.astream(model="...", messages=[...]):
    ...
```

## Audit endpoint

```
POST /v1/audit/record-llm
{
  "tool_id": "llm.<provider>",       # synthesised by the SDK
  "model": "<provider model name>",
  "input_tokens": int,
  "output_tokens": int,
  "cost_usd": float,
  "duration_ms": int,
  "workspace_id": str | null,
  "agent_id": str | null,
  "finish_reason": str | null
}
→ 201 { "audit_id": "evt_..." }
```

The endpoint synthesises an `audit_events` row with `tool_id="llm.<provider>"`
so existing dashboards keying on the audit log automatically pick up
direct-LLM cost. Audit failure never breaks the LLM call (the SDK
swallows the POST failure).

## Pricing tables

`plinth.llm_providers.anthropic.ANTHROPIC_PRICING` covers
`claude-sonnet-4-5`, `claude-opus-4-5`, `claude-haiku-4-5`.
`plinth.llm_providers.openai.OPENAI_PRICING` covers `gpt-5`,
`gpt-5-mini`, `gpt-5-nano`. Unknown models fall back to a sensible
mid-tier rate so cost estimates stay non-zero during model rollouts.

## Backwards compatibility (v1.2)

- All v1.0/v1.1 endpoints unchanged.
- `client.llm` is a new namespace — no existing call sites are touched.
- LLM extras are opt-in: `pip install plinth` keeps the dependency tree
  identical to v1.1.
- The TypeScript SDK does not gain an LLM layer in v1.2.

# v1.3 Additions — Cluster-Mode Workspace Lease Coordination

v1.3 closes the v1.1 known limitation: workspace lease coordination is now
cluster-shared via `CoordinationBackend` (memory by default, Redis when
configured). Multiple workspace replicas can safely race for the same
workflow step — exactly one wins.

## Lease coordination model

When `PLINTH_COORDINATION_BACKEND=redis` is set on the workspace service,
`LeaseStore.acquire_lease(...)` does two things:

1. **Cluster gate**: tries to acquire a distributed lock keyed
   `<prefix>:workspace:lease:<workspace_id>:<step_id>` with TTL = lease TTL.
   If lost, raises `LeaseConflict` immediately (no DB work). The error
   `details` include `cluster_key` so operators can trace cluster-side
   contention.

2. **Local upsert** (existing SQLite/Postgres path) for the
   `workflow_step_leases` row.

`LeaseStore.heartbeat_lease(...)` refreshes both the cluster lock TTL and
the local row.

`LeaseStore.release_lease(...)` deletes both.

`LeaseStore.expire_stale_leases(...)` (the reaper) sweeps both: stale rows
flip to `expired` and the matching cluster lock is released best-effort.

If the local DB write fails after cluster acquisition succeeds, the
cluster lock is released defensively — no orphaned cluster locks even on
errors.

## Resource locks

The same v0.6 generic `ResourceLockStore.acquire(...)` primitive gets the
identical treatment: cluster-gate first, local upsert second, with the
prefix `<prefix>:workspace:resource_lock:<workspace_id>:<name>`. Heartbeat
and release refresh / drop the cluster lock. The reaper releases cluster
locks for swept rows.

## Backwards compatibility (v1.3)

- All v1.0/v1.1/v1.2 endpoints unchanged.
- `coordination_backend=memory` (default): cluster gate is short-circuited.
  v1.2 single-process behaviour is preserved bit-for-bit.
- `coordination=None` (legacy callers that construct `LeaseStore` directly
  with one positional argument): identical to v1.0 behaviour.
- All 33 existing lease tests and all 30 resource-lock tests continue to
  pass with the new code path.
- API surface unchanged (no new endpoints; the cluster gate is internal
  to `LeaseStore` / `ResourceLockStore`).
- Worker / SDK code unchanged.

## Operator note

For multi-replica workspace deployments, set:

```
PLINTH_COORDINATION_BACKEND=redis
PLINTH_COORDINATION_REDIS_URL=redis://<cluster-redis>:6379/0
PLINTH_COORDINATION_KEY_PREFIX=plinth-prod
```

Without Redis, multiple replicas will race in the database layer only —
race-safe per-row but not coordinated across replicas; recommended only
for read-replicas + a single primary writer.

# v1.4 Additions — Per-Agent Cost & Anomaly Detection

v1.4 introduces two new read-only views over the gateway's audit log:

1. **Per-agent cost rollup** — aggregate cost / invocations / tools by
   agent over a window.
2. **Anomaly detection** — detector-driven scan of the audit log for
   cost spikes, rate spikes, error spikes, new-tool first uses, and
   unusual sequences.

Both surfaces are additive — no existing endpoint changes shape, no
schema migrations, no new tables. The detector runs in-process over
the existing `audit_events` table.

## Endpoints (gateway)

### `GET /v1/audit/cost-by-agent`

Aggregate cost + invocations + per-tool breakdown by agent over a
window.

**Query parameters:**

- `window` (default `"24h"`) — one of `"1h"`, `"24h"`, `"7d"`, `"30d"`
  or any matching shape (`"30m"`, `"60s"` etc. accepted by
  `parse_window`). Anything else returns `400 INVALID_ARGUMENTS`.
- `tenant_id` (optional) — only respected in permissive mode; strict
  auth modes always pin to the caller's tenant.
- `top` (default `10`, range `1..200`) — maximum number of agent rows
  returned.

**Response:** `200 CostByAgentReport`

```json
{
  "window": "24h",
  "window_start": "2026-05-09T12:00:00+00:00",
  "window_end": "2026-05-10T12:00:00+00:00",
  "agents": [
    {
      "agent_id": "ag_a",
      "tenant_id": "default",
      "invocations": 42,
      "cached_invocations": 12,
      "total_cost_usd": 0.42,
      "avg_duration_ms": 132.5,
      "top_tools": [
        {"tool_id": "web.fetch", "invocations": 30, "cost_usd": 0.30},
        {"tool_id": "web.search", "invocations": 12, "cost_usd": 0.12}
      ]
    }
  ],
  "total_agents": 1,
  "total_cost_usd": 0.42,
  "fetched_at": "2026-05-10T12:00:00+00:00"
}
```

`AgentCost` rows where `agent_id IS NULL` are bucketed under the
sentinel `agent_id="(unknown)"` so the row stays visible in
dashboards. `total_agents` is the unfiltered distinct agent count over
the window (so dashboards can show "showing top N of M");
`total_cost_usd` is the sum across ALL agents — not just the top-N
returned.

### `GET /v1/audit/anomalies`

Run the detector suite over the audit log.

**Query parameters:**

- `window` (default `"1h"`) — same parser as cost-by-agent. Governs
  the focus span; the detector always uses a fixed 60-minute baseline.
- `min_severity` (default `"info"`) — `"info" | "warning" |
  "critical"`. Lower severities are dropped from the response.
- `type` (optional) — restrict to one of `cost_spike`, `rate_spike`,
  `error_spike`, `new_tool`, `unusual_pattern`. Anything else returns
  `400 INVALID_ARGUMENTS`.
- `agent_id` (optional) — restrict the focus window to one agent.

**Response:** `200 AnomalyReport`

```json
{
  "detected_at": "2026-05-10T12:00:00+00:00",
  "window": "1h",
  "anomalies": [
    {
      "id": "anom_01HZ...",
      "type": "cost_spike",
      "severity": "critical",
      "agent_id": "ag_a",
      "tenant_id": "default",
      "tool_id": null,
      "detected_at": "2026-05-10T12:00:00+00:00",
      "window_start": "2026-05-10T11:00:00+00:00",
      "window_end": "2026-05-10T12:01:00+00:00",
      "description": "agent ag_a cost $5.0000 in 1-minute window vs baseline $0.0001±$0.0000",
      "metric_name": "cost_usd_per_minute",
      "metric_value": 5.0,
      "baseline_mean": 0.0001,
      "baseline_stddev": 0.0,
      "z_score": 100.0,
      "raw_data": {
        "minute": "2026-05-10T11:59:00+00:00",
        "baseline_samples": [0.0, 0.0, 0.0]
      }
    }
  ],
  "total_anomalies": 1,
  "by_severity": {"critical": 1}
}
```

Results are cached in-process for **30 seconds** keyed by
`(window, type, min_severity, agent_id, tenant_id)`. Dashboard polling
(every 30s) hits the cache exactly once per refresh cycle.

## Detector tunables (defaults)

These constants live in `services/gateway/src/plinth_gateway/anomaly.py`.
Operators tune by editing the module, not via env vars (yet) — they're
chosen conservatively to balance noise vs detection latency.

| Constant | Default | Meaning |
|---|---|---|
| `Z_WARNING` | `2.0` | z-score threshold for `warning` (cost / rate). |
| `Z_CRITICAL` | `3.0` | z-score threshold for `critical` (cost / rate). |
| `ERR_MULT_WARNING` | `5.0` | error_spike: focus errors / baseline mean ≥ 5x. |
| `ERR_MULT_CRITICAL` | `10.0` | error_spike: ≥ 10x. |
| `MIN_ERRORS` | `5` | error_spike floor — fewer errors → no fire. |
| `FOCUS_MINUTES` | `1` | Width of the most-recent slice scored. |
| `BASELINE_MINUTES` | `60` | Trailing window used for mean/stddev. |
| `LOOKBACK_HOURS` | `24` | Trailing window for new_tool / unusual_pattern. |
| `CACHE_TTL_SECONDS` | `30.0` | In-process anomaly-report cache lifetime. |

## Detector specification (informational)

- **`cost_spike`** — per-(agent, minute) sum of `cost_estimate_usd`.
  Baseline = same agent's per-minute costs over the trailing
  `BASELINE_MINUTES`, padded with zeros for missing minutes. Severity
  derived from |z|.
- **`rate_spike`** — per-(agent, minute) `COUNT(*)`. Same baseline +
  thresholds as cost_spike.
- **`error_spike`** — per-(tool, minute) `SUM(error IS NOT NULL)`.
  Multiplicative threshold against baseline mean; minimum
  `MIN_ERRORS` errors required to even consider firing.
- **`new_tool`** — emit `info` for any (agent, tool) pair appearing in
  the focus window but absent from the trailing 24h.
- **`unusual_pattern`** — emit `info` when an agent's per-minute
  `sha256("|".join(sorted(tool_ids)))` differs from every prior minute
  in the trailing 24h.

## SDK additions (Python)

```python
client = Plynf(api_key="local-dev")

# Per-agent cost rollup over the last 24h.
report = client.gateway.cost_by_agent(window="24h", top=10)
for agent in report.agents:
    print(agent.agent_id, agent.total_cost_usd)

# Anomalies in the last hour, warning+ only.
anoms = client.gateway.anomalies(window="1h", min_severity="warning")
for a in anoms.anomalies:
    print(a.type, a.severity, a.description)
```

Both methods round-trip through `client.tools` and `client.gateway`
(the v0.5 alias). New typed models exported from `plinth`:

- `AgentCost`, `ToolUsage`, `CostByAgentReport`
- `Anomaly`, `AnomalyReport`

## Dashboard additions

Two new panels on the overview page (`#/`):

- **Cost by agent** — top 10 agents in the last 24h, sortable by
  cost / invocations / avg duration. Each row shows a stacked bar of
  the top 5 tools. "Audit" button opens a modal with the agent's
  recent invocations.
- **Anomalies** — collapsible list of anomalies with severity glyph,
  metric + z-score, agent/tenant/tool dimensions, and a small SVG
  sparkline of the baseline window (when available in `raw_data`).

Both panels poll their respective `/api/cost-by-agent` and
`/api/anomalies` proxies every **30 seconds** — independent from the
5-second overview poll so the heavier queries don't compete with the
main refresh.

## Backwards compatibility (v1.4)

- Every existing endpoint is byte-for-byte unchanged.
- No schema migration — the detector reads `audit_events` only.
- `client.tools` / `client.gateway` get two new methods; existing
  methods unchanged.
- `__all__` in `plinth.__init__` gains 5 new symbols (additive).
- Dashboard SPA: existing panels untouched. New panels render even
  when zero agents / zero anomalies are returned.

## Operator note

The detector's tunables are deliberately conservative. If you see a
flood of `info` anomalies on a busy gateway (most likely
`unusual_pattern` from genuinely diverse traffic), raise the
`min_severity` filter on the dashboard URL or call the SDK with
`min_severity="warning"`. To tune the underlying thresholds, edit
`anomaly.py` and redeploy the gateway — changes take effect on the
next request after the 30-second cache TTL elapses.

# v1.5 Additions — Workflow Visualization v2 + Plynf Studio MVP

v1.5 ships two related dashboard chunks that build on the existing v1.0
workflow viz: a historical *replay* view for any workflow, and a
visual-builder *studio* for composing workflows without code. Both are
additive — every previous endpoint is byte-for-byte unchanged.

## Workflow Replay (Dashboard route `/workflows/{id}/replay`)

A new SPA route renders three stacked panels for any workflow:

1. **Timeline scrubber** — horizontal SVG axis from the workflow's
   `created_at` to `finished_at` (or `now` for active workflows). One
   tick mark per step state-change event derived from the workflow's
   step rows. Dragging the slider sets a *cursor timestamp*; the SPA
   reconstructs each step's state by replaying every event with
   `ts <= cursor`.
2. **Step state at scrub position** — re-uses the v1.0 graph renderer
   to show the step nodes coloured as they were at the cursor moment
   (`pending` / `running` / `completed` / `failed` / `cancelled`). The
   live `/workflows/{id}` route is unchanged.
3. **Error attribution** — for any step that failed at least once,
   shows the failing attempt's `error` message, the input that was
   passed in, and a per-attempt list (`attempt 1 — failed @ 12:00:07
   — model timeout`).

A **"Restore workspace to this point"** button surfaces the latest
snapshot at-or-before the cursor and shows the operator the snapshot
ID + the workspace API call needed to actually restore it. (The
restore *itself* is not automated by the dashboard — it would silently
mutate workspace state.)

### Replay aggregator

```
GET /api/workflows/{wf_id}/replay?ws=<ws_id>   → 200 ReplayPayload
```

Aggregates three upstream calls in parallel:

- `GET {workspace}/v1/workspaces/{ws_id}/workflows/{wf_id}` (required)
- `GET {workspace}/v1/workspaces/{ws_id}/snapshots` (best-effort)
- `GET {gateway}/v1/audit?workspace_id={ws_id}&limit=1000` (best-effort)

`ReplayPayload`:

```json
{
  "workflow":      { /* full Workflow row */ },
  "snapshots":     [ /* Snapshot rows */ ],
  "audit_events":  [ /* AuditEvent rows */ ],
  "timeline": [
    {
      "ts": "2026-05-10T12:00:01+00:00",
      "kind": "workflow.created" | "workflow.started" | "workflow.finished" |
              "step.created" | "step.started" | "step.finished",
      "step_name": "search",   // omitted on workflow.* events
      "step_id":   "step_xxx",
      "attempt":   1,
      "status":    "pending" | "running" | "completed" | "failed" | "cancelled",
      "error":     null
    }
  ]
}
```

The audit log does *not* track `workflow_id` today, so the timeline
events are reconstructed from the workflow's own step rows
(`created_at` / `started_at` / `finished_at` / `attempt` / `status`).
This keeps the replay view honest: it only shows what the workspace
itself recorded. The audit array is returned alongside so the SPA can
correlate tool calls by timestamp if it wants to.

If the snapshot or audit fetch fails, the corresponding array is
returned empty — the timeline reconstruction never blocks.

### Replay route lifecycle

The replay view *disables auto-refresh* while the user is scrubbing —
the cached payload is the source of truth, and a 5-second poll would
either clobber the cursor position or flood the workspace with calls.
Operators who want a live view click the **"live view"** link in the
header which navigates to the existing `/workflows/{id}` route.

## Plynf Studio MVP (Dashboard route `/studio`)

A visual workflow builder. Three-pane layout:

- **Toolbox (left)** — five step-type buttons: `tool` / `llm` /
  `channel_send` / `channel_receive` / `manual`. Clicking a button
  appends a new step at the end of the canvas and opens the per-step
  config modal so the user can fill in required fields immediately.
- **Canvas (center)** — vertical numbered list of steps. Each row has
  `↑` / `↓` reorder buttons, an `edit` button (re-opens the config
  modal), and an `×` remove button. Steps are coloured red while they
  have missing required fields.
- **Properties panel (right)** — a form for the workflow's `name`,
  `description`, `retry_policy`, and `max_attempts_default`. The save
  button writes the canvas + properties as a `WorkflowDefinition`
  document and POSTs it to the workspace.

> **Spec deviation: drag-drop fallback.** The original spec called for
> drag-drop between the toolbox and the canvas. With no external libs
> that's a fragile DOM-event maze; per the spec's own fallback note we
> ship "click to insert + ↑ ↓ reorder" instead. The serialised JSON
> definition shape is unchanged.

### `WorkflowDefinition` JSON shape

The definition is the contract between the studio canvas, the import
endpoint, and the SDK helper. Step schemas are *advisory* — the
workspace validates only the envelope; tool_id references are NOT
resolved against the gateway registry (a workflow that names an
unknown tool is still a valid object — the failure surfaces at run
time when the worker invokes it).

```json
{
  "name": "lead-research-pipeline",
  "description": "Research a lead and extract facts.",
  "retry_policy": "exponential",
  "max_attempts_default": 3,
  "metadata": { "owner": "alice" },
  "steps": [
    {
      "name": "search",
      "type": "tool",
      "tool_id": "web.search",
      "arguments_template": {"query": "{input.topic}", "k": 5},
      "max_attempts": 3
    },
    {
      "name": "extract",
      "type": "llm",
      "model": "claude-sonnet-4-5",
      "system": "You are a research assistant.",
      "prompt_template": "Extract facts from:\n{step.search.output}",
      "max_attempts": 2
    },
    {
      "name": "publish",
      "type": "channel_send",
      "channel": "research-out",
      "payload_template": {"facts": "{step.extract.output}"},
      "max_attempts": 1
    }
  ]
}
```

Recognised step `type` values: `tool`, `llm`, `channel_send`,
`channel_receive`, `manual`. `manual` is a placeholder for human-in-
loop (post-v1.5); it has no config fields.

### Studio buttons

- **Save** — POSTs the definition to
  `/api/workspaces/{ws_id}/workflows/import` (proxy → workspace).
  On success, redirects to `/#/workflows/{wf_id}/replay?ws=...` so the
  user can immediately start (or run) the new workflow.
- **Export JSON** — downloads the canvas as a `.plinth.json` file for
  offline editing or version control.
- **Load** — fetches the workspace's existing workflows. Workflows
  imported via studio carry the full definition under
  `metadata['definition']` and round-trip cleanly. Legacy workflows
  (created via `WorkflowsProxy.create`) load as a manual-step skeleton
  that the user can re-edit.

## Workspace import endpoint

```
POST /v1/workspaces/{ws_id}/workflows/import
  body: WorkflowDefinition
  → 201 Workflow
  → 400 InvalidArguments
  → 404 WorkspaceNotFound
```

Implementation lives at `WorkflowStore.import_workflow(ws_id, def)`.
The endpoint:

1. Validates the envelope (`name` non-empty, `steps` non-empty array,
   each step has a non-empty `name` + an optional but recognised
   `type`, step names are unique within the workflow).
2. Calls `create_workflow()` with `steps_manifest = [s["name"] for s
   in definition.steps]`.
3. Stores the *full* definition in `workflow.metadata["definition"]`
   plus `workflow.metadata["imported_via"] = "plinth-studio"` so the
   studio's load button can re-hydrate the canvas.
4. Returns the freshly-created `Workflow` (no steps started — the
   caller drives the lifecycle through the usual `create_step` /
   `update_step` endpoints).

The new endpoint participates in the same per-workspace
`max_workflows_per_workspace` quota as `create_workflow` — importing
does not bypass tenant quotas.

## Dashboard endpoints

Two new endpoints in addition to the SPA shell routes:

| Method + path | Purpose |
|---|---|
| `GET /api/workflows/{wf_id}/replay?ws=<ws_id>` | Replay aggregator (above). |
| `POST /api/workspaces/{ws_id}/workflows/import` | Thin proxy to the workspace import endpoint. |
| `GET /studio` | SPA shell for `/#/studio`. |
| `GET /workflows/{wf_id}/replay` | SPA shell for `/#/workflows/{id}/replay`. |

The proxy follows the standard `_proxy_mut` pattern: forwards the body
verbatim, returns the upstream status + body verbatim, surfaces 502 on
upstream connection errors. The replay aggregator is the only
endpoint that joins multiple upstreams; failures of the
audit/snapshot calls degrade the payload (empty arrays) rather than
failing the whole request.

## Python SDK additions

```python
# Plynf Studio import — round-trips a JSON definition through the
# workspace import endpoint and returns a WorkflowHandle ready for the
# normal step lifecycle.
wf = ws.workflows.import_definition({
    "name": "lead-research-pipeline",
    "retry_policy": "exponential",
    "max_attempts_default": 3,
    "steps": [
        {"name": "search", "type": "tool",
         "tool_id": "web.search",
         "arguments_template": {"query": "{input.topic}"}},
        {"name": "extract", "type": "llm",
         "model": "claude-sonnet-4-5",
         "system": "You are a research assistant.",
         "prompt_template": "Extract from:\n{step.search.output}"},
    ],
})
assert wf.steps_manifest == ["search", "extract"]
```

The TypeScript SDK is unchanged for v1.5 (Python only).

## Backwards compatibility (v1.5)

- Every previous endpoint is byte-for-byte unchanged.
- `WorkflowStore.create_workflow()` and the `POST .../workflows`
  endpoint behave exactly as in v1.4.
- The new `/import` route is registered before the
  `GET /workflows/{wf_id}` route in the workspace API; FastAPI's
  method-aware matcher disambiguates them.
- `Workflow.metadata` was always a free-form dict; storing the
  `definition` + `imported_via` keys does not change its schema.
- Dashboard SPA: existing routes (`/`, `/workspaces/{id}`,
  `/workflows`, `/workflows/{id}`, `/tenants`, `/tenants/{id}`) all
  unchanged. The two new routes (`/studio`, `/workflows/{id}/replay`)
  are added to the topnav but do not affect any existing markup.
- The 5-second polling cadence on the live workflow detail / list
  pages is unchanged. The new replay route deliberately *disables*
  auto-refresh while scrubbing.
- Python SDK: `WorkflowsProxy` gains one method
  (`import_definition`); existing methods unchanged. `__all__` of
  `plinth.workflows` is unchanged (the new method is exposed via the
  proxy class).

# v1.5 Additions — Atlassian, Salesforce, Asana MCP Servers

Three new OAuth-backed MCP servers ship in v1.5, expanding the catalog
of first-party integrations to cover ticket tracking, CRM, and project
management workflows.

## Atlassian MCP Server (NEW — port 7431)

Provides agent access to Jira and Confluence via Atlassian's 3LO OAuth flow.

Tools:
- `atlassian.jira_search` — JQL search → list of issues
- `atlassian.jira_get_issue` — full issue with comments
- `atlassian.jira_create_issue` — new issue with ADF description
- `atlassian.jira_update_issue` — edit fields on an issue
- `atlassian.jira_comment` — append a comment
- `atlassian.confluence_search` — CQL search across pages
- `atlassian.confluence_get_page` — page with storage-format body
- `atlassian.confluence_create_page` — create a page in a space

OAuth: standard Atlassian 3LO flow. The authorize URL carries the mandatory
`audience=api.atlassian.com` parameter. PKCE on. Default scopes:
`read:jira-work write:jira-work read:confluence-content.summary
write:confluence-content offline_access`.

After token exchange the gateway calls
`https://api.atlassian.com/oauth/token/accessible-resources` and stores the
first workspace's `id` as `connection.metadata.cloudid`. The MCP server reads
it from the `X-Plynf-OAuth-Cloudid` header that the gateway proxy forwards
on every invoke and addresses Jira/Confluence via the
`/ex/jira/{cloudid}/...` and `/ex/confluence/{cloudid}/wiki/...` routes.

## Salesforce MCP Server (NEW — port 7432)

Provides agent access to Salesforce REST.

Tools:
- `salesforce.soql_query` — run a SOQL query
- `salesforce.get_record` — fetch a single record
- `salesforce.create_record` — create Lead/Contact/Opportunity/...
- `salesforce.update_record` — PATCH fields onto a record
- `salesforce.delete_record` — delete a record
- `salesforce.list_objects` — list SObject types + their schema

OAuth: standard Salesforce OAuth (login.salesforce.com). PKCE on. Default
scopes: `api refresh_token offline_access`.

The token-exchange response includes `instance_url` (the per-org REST API
base, e.g. `https://acme.my.salesforce.com`). The gateway captures this into
`connection.metadata.instance_url` and forwards it as
`X-Plynf-OAuth-InstanceUrl` on every proxied invoke. The MCP server reads
the header and uses it as the per-call API base
(`{instance_url}/services/data/{api_version}/...`).

The MCP server validates the inbound `instance_url` is HTTPS and matches a
known Salesforce-domain suffix (`*.salesforce.com`, `*.force.com`, etc.)
before using it, blocking malicious header injection.

## Asana MCP Server (NEW — port 7433)

Provides agent access to Asana workspaces, projects, and tasks.

Tools:
- `asana.list_workspaces` — accessible workspaces
- `asana.list_projects` — projects in a workspace
- `asana.list_tasks` — tasks in a project
- `asana.get_task` — task with project membership
- `asana.create_task` — task in workspace or projects
- `asana.update_task` — edit name/notes/completed/assignee/due_on

OAuth: standard Asana OAuth flow. PKCE on. Default scope: `default`.

No per-connection metadata is required — Asana tokens are workspace-scoped
implicitly via the user.

## Gateway changes

`OAuthConnection` now has a `metadata: dict[str, Any]` field (persisted as
JSON in the new `oauth_connections.metadata` TEXT column). It is populated
from provider-specific data captured during OAuth callback (Atlassian's
cloudid, Salesforce's instance_url) and round-trips through
`POST/GET /v1/oauth/connections`.

`OAuthProvider` gains an `extra_authorize_params: dict[str, str] | None`
field for provider-specific authorize-URL query params (Atlassian needs
`audience=api.atlassian.com`).

The gateway proxy's OAuth resolver returns
`(auth_header, metadata_headers_dict)` rather than a single header. The
metadata mapping table is:

| Provider | Connection metadata key | Outbound header           |
|----------|-------------------------|---------------------------|
| Atlassian | `cloudid`              | `X-Plynf-OAuth-Cloudid`  |
| Salesforce | `instance_url`        | `X-Plynf-OAuth-InstanceUrl` |

Other providers emit no extra headers.

## Migration

`migrations/0006_oauth_metadata.sql` adds the `metadata TEXT` column. The
in-line `db.py` schema bootstrap and `_migrate()` both handle the column
idempotently for fresh and upgraded databases.

## Stack additions (v1.5 OAuth)

No new runtime dependencies. The three MCP servers reuse the existing
FastAPI / httpx / structlog stack the other MCP servers use.

## Backwards compatibility (v1.5 OAuth)

- All v1.0–v1.5 endpoints unchanged.
- `OAuthConnectionPublic.metadata` defaults to `{}` for connections created
  before v1.5; existing API consumers see the new key in JSON responses but
  can ignore it.
- Existing OAuth providers (GitHub, Slack, Linear, Notion, Google) emit no
  new headers and behave identically to v1.4.
- The migration is purely additive — the column is nullable so a rollback to
  v1.4 leaves prior rows readable (the v1.4 code never touches the column).

