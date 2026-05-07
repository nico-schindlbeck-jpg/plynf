# Plinth — Internal API Contracts

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
from plinth import Plinth

client = Plinth(
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
import { Plinth } from "@plinth/sdk";

const client = new Plinth({
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
client = Plinth(
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
client_with_token = Plinth(workspace_url=..., gateway_url=..., api_key=token.token)

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

Both servers behave identically to the GitHub MCP server: read OAuth bearer from forwarded `Authorization` header, return Plinth-shaped error envelopes.

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
