# Plynf — Python SDK

The official Python client for [Plynf](../../README.md), the agent-native
runtime layer.

> **TL;DR:** A versioned workspace + tool gateway, wrapped in an ergonomic Python
> client so your agent's state and tool calls are persistent, auditable, and
> cheap to replay.

```bash
pip install plinth
```

```python
from plinth import Plynf

client = Plynf(api_key="local-dev")
ws = client.workspace("research-task-1")
ws.kv.set("topic", "renewable energy")
result = client.tools.invoke("web.fetch", {"url": "mock://ipcc-2023"})
ws.snapshot("baseline")
```

That's it. Five lines, persistent state, audited tools.

---

## Table of contents

- [Why Plynf?](#why-plinth)
- [Installation](#installation)
- [Quickstart](#quickstart)
- [Workspaces](#workspaces)
  - [Versioned KV](#versioned-kv)
  - [Versioned files](#versioned-files)
  - [Snapshots](#snapshots)
  - [Branches](#branches)
  - [Channels](#channels)
  - [Workflows](#workflows)
- [Tools](#tools)
  - [Invoke](#invoke)
  - [Dry-run](#dry-run)
  - [Register](#register)
  - [Audit & stats](#audit--stats)
- [Token counting](#token-counting)
- [Agent decorator](#agent-decorator)
- [Error handling](#error-handling)
- [Configuration reference](#configuration-reference)
- [Development](#development)

---

## Why Plynf?

Today's agents are wrapped around interfaces designed for humans — clicking
buttons, parsing screenshots, re-reading the same content from chat history.
Plynf flips the model: the agent is the first-class user. We give it a
persistent versioned workspace and a single tool gateway, both designed for
machines first.

This SDK is the way Python agents talk to that runtime. It is intentionally
small and predictable — every method maps to one HTTP call, every error to a
typed exception, every model to a Pydantic class.

## Installation

```bash
pip install plinth
```

Requires Python 3.11+.

## Quickstart

Spin up the services (see the project [README](../../README.md) for `make
serve`), then:

```python
from plinth import Plynf

client = Plynf(
    workspace_url="http://localhost:7421",
    gateway_url="http://localhost:7422",
    api_key="local-dev",          # any non-empty string in dev
    timeout=30.0,
)

# Get-or-create a workspace by name.
ws = client.workspace("research-task-1")

# Versioned KV.
ws.kv.set("topic", "renewable energy")
print(ws.kv.get("topic"))                 # → "renewable energy"

# Versioned files.
ws.files.write("report.md", "# Report\n...")
print(ws.files.read("report.md", as_text=True))

# Snapshot the state.
snap = ws.snapshot("baseline", message="initial state")

# Tool calls (audited, cached).
result = client.tools.invoke("web.fetch", {"url": "mock://ipcc-2023"})
print(result.cached, result.duration_ms)
```

## Workspaces

A *workspace* is the top-level isolation boundary for one agent's state.
`client.workspace(name)` returns a handle, creating the workspace on the
server if it does not yet exist:

```python
ws = client.workspace("research-task-1")
ws.id        # → "ws_01H..."
ws.name      # → "research-task-1"
```

If you already have a workspace ID:

```python
ws = client.get_workspace("ws_01H...")
```

### Versioned KV

Every write creates a new immutable version. Reads return the latest by
default; pass `version=N` to read a specific revision.

```python
# Write — returns a KVEntry.
entry = ws.kv.set("topic", "renewable energy")
print(entry.version)          # → 1

# Read — value only.
ws.kv.get("topic")            # → "renewable energy"

# Read — value + version.
value, version = ws.kv.get("topic", with_version=True)

# Read — full KVEntry.
entry = ws.kv.get("topic", with_meta=True)

# Read — specific version.
ws.kv.get("topic", version=1)

# Default for missing keys (no exception).
ws.kv.get("nope", default=None)

# History — every version, oldest first.
for entry in ws.kv.history("topic"):
    print(entry.version, entry.value)

# List the latest version of every key.
for entry in ws.kv.list():
    print(entry.key)

# Delete (creates a tombstone version).
ws.kv.delete("topic")
```

### Versioned files

Same model as KV, but for byte-addressable content with paths:

```python
# Strings are auto-encoded UTF-8.
ws.files.write("report.md", "# Report\n...")

# Bytes — pass content_type for accurate metadata.
ws.files.write("data.bin", b"\x00\x01\x02", content_type="application/octet-stream")

# Read raw bytes…
content: bytes = ws.files.read("report.md")

# …or text (UTF-8 by default).
text: str = ws.files.read("report.md", as_text=True)

# Metadata only (no body download).
meta = ws.files.meta("report.md")
print(meta.size, meta.sha256, meta.content_type)

# Delete.
ws.files.delete("old.md")

# List every file.
for f in ws.files.list():
    print(f.path, f.size)
```

### Snapshots

A snapshot freezes the latest version of every key and file in the workspace.
They are immutable and cheap (they reference versions, not data).

```python
snap = ws.snapshot("baseline", message="initial state")
snap.id                        # → "snap_01H..."

# List all snapshots in this workspace.
for s in ws.snapshots():
    print(s.name, s.created_at)

# Diff two snapshots.
diff = ws.diff(snap.id, other_snap_id)
print(diff.kv_added, diff.files_modified)
```

### Branches

A branch is a divergent timeline forked from a snapshot. Writes against a
branch don't affect main until merged.

```python
# Fork from a snapshot.
branch = ws.branch("experiment", from_snapshot=snap.id)

# Get a Workspace view scoped to the branch — every KV/file/snapshot
# call automatically targets this branch.
ws_branch = ws.with_branch(branch.id)
ws_branch.kv.set("topic", "alternative")

# Main is unchanged.
ws.kv.get("topic")             # → original value

# Merge when you're ready.
result = ws.merge(branch.id)
print(result.kv_keys_merged)

# Or list / delete branches.
for b in ws.branches():
    print(b.name, b.merged)
ws.delete_branch(branch.id)
```

### Channels

A *channel* is a workspace-scoped, persistent message queue with monotonic
per-channel sequence numbers. Use them for hand-offs between agents
(research → writer → reviewer pipelines, etc.). Channels are created lazily
on the first `send` and survive process restarts.

```python
# Send a message — the channel is created on demand.
msg = ws.channels.send(
    "research-out",                      # channel name
    {"sources": [...], "facts": {...}},  # payload (any JSON-serialisable)
    sender="researcher",                 # optional descriptive label
    type="research.complete",            # optional message type
    correlation_id=None,                 # optional request/response correlation
    headers={},                          # optional string-string metadata
)
print(msg.id, msg.seq, msg.sent_at)

# Receive messages. Pass a `consumer` to track a per-consumer cursor on
# the server, so subsequent calls without `since=` resume where you left off.
msgs = ws.channels.receive(
    "research-out",
    consumer="writer",   # optional
    since=None,          # optional — explicit seq override (returns seq > since)
    limit=100,           # default 100, max 1000
    peek=False,          # set True to leave the cursor untouched
)
for msg in msgs:
    process(msg.payload)
    ws.channels.ack(msg)             # delete (also: ws.channels.delete(msg))

# Block-wait for one message — polls under the hood.
msg = ws.channels.wait(
    "research-out",
    consumer="writer",
    timeout=30.0,        # seconds; returns None on timeout
    poll_interval=0.5,
)

# Channel management.
for ch in ws.channels.list():       # → list[Channel]
    print(ch.name, ch.message_count)

channel = ws.channels.get("research-out")   # → Channel
ws.channels.delete_channel("research-out")  # remove channel + all messages
```

`ws.channels.ack` accepts a full `ChannelMessage` (preferred). Passing only a
bare ID raises `ValueError` because the channel name is required to build the
DELETE URL — receive the message first, then ack the object you got back.

### Workflows

A *workflow* is a named manifest of expected step names plus a server-tracked
log of completed steps. Each step has a lifecycle of `pending → running →
(completed | failed | cancelled)` and may reference a workspace snapshot at
completion time so a crashed agent can resume from a known checkpoint.

```python
# Create a workflow with a step manifest.
wf = ws.workflows.create(
    name="research-pipeline",
    steps=["search", "fetch", "extract", "synthesize"],
    metadata={"topic": "renewable energy"},
)
# → WorkflowHandle, with `.id`, `.name`, `.status`, `.steps`, `.steps_manifest`.

# Idempotent: get the existing one if it's already there.
wf = ws.workflows.get_or_create(
    "research-pipeline",
    steps=["search", "fetch", "extract", "synthesize"],
)

# Attach to an existing workflow by ID.
wf = ws.workflows.get(wf.id)              # → WorkflowHandle

# Per-step lifecycle.
step = wf.start_step("search", input={"topic": "renewable energy"})
# … work happens, the workspace mutates …
snap = ws.snapshot("after-search")
wf.complete_step(step.id, output={"sources": [...]}, snapshot_id=snap.id)

# Errors / cancellation.
wf.fail_step(step.id, error="connection refused")
wf.cancel_step(step.id)
wf.cancel()                                # cancel the entire workflow

# Resume after crash — pick up at the next pending step from the last
# completed snapshot.
resume = wf.resume_info()                  # → ResumeInfo
if resume.next_step:
    # Restore the workspace from `resume.snapshot_id`, then continue at
    # `resume.next_step`.
    ...

# Re-fetch the workflow (and its full step log) from the server.
wf.refresh()
print(wf.status, wf.steps)

# List every workflow on the workspace.
for w in ws.workflows.list():              # → list[Workflow]
    print(w.id, w.name, w.status)
```

`WorkflowHandle.start_step` rejects step names that are not in the workflow's
manifest (raising `InvalidWorkflowStep` *before* the request leaves the box).
The same exception is mapped from the server-side `INVALID_WORKFLOW_STEP`
error code.

## Tools

`client.tools` is the gateway client. Tool invocations go through the gateway,
which adds caching, idempotency, and a complete audit trail.

### Invoke

```python
# Simple invoke — caching is on by default.
result = client.tools.invoke("web.fetch", {"url": "mock://ipcc-2023"})
print(result.result)           # tool-specific payload
print(result.cached)           # bool — was this a cache hit?
print(result.duration_ms)
print(result.cost_estimate_usd)

# Disable cache for a single call.
result = client.tools.invoke("web.search", {"query": "x"}, cache=False)

# Attribute the call to a workspace + agent for audit purposes.
client.tools.invoke(
    "web.fetch",
    {"url": "..."},
    workspace_id=ws.id,
    agent_id="agent-007",
    idempotency_key="dedup-123",
)
```

### Dry-run

Predict whether the call would hit cache and what it would cost, *without*
actually invoking the tool:

```python
plan = client.tools.dry_run("web.fetch", {"url": "..."})
print(plan.would_invoke, plan.cached_result, plan.estimated_cost_usd)
```

### Register

```python
from plinth import ToolRegistration

client.tools.register(
    ToolRegistration(
        tool_id="my.tool",
        name="My Tool",
        description="Action-oriented description.",
        transport="http",
        endpoint="http://my-tool.local/invoke",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        output_schema={"type": "object"},
        idempotent=True,
        side_effects="read",
        cache_ttl_seconds=600,
    )
)

# Or pass a plain dict — the SDK validates it for you.
client.tools.register({"tool_id": "my.tool", ...})

# List, fetch, deregister.
for tool in client.tools.list():
    print(tool.tool_id)
client.tools.get("web.fetch")
client.tools.deregister("my.tool")
```

### Audit & stats

```python
# Audit log — relative durations or ISO timestamps.
events = client.tools.audit(workspace_id=ws.id, since="1h", limit=100)
events = client.tools.audit(since="2026-01-01T00:00:00Z")

# Aggregate stats per workspace.
stats = client.tools.stats(workspace_id=ws.id)

# Cache stats / clear.
client.tools.cache_stats()                 # {"hits": ..., "misses": ..., "size_bytes": ...}
client.tools.clear_cache(tool_id="web.fetch")
```

## Token counting

Plynf ships with offline token counting via `tiktoken`'s `cl100k_base`
encoding, which is a close-enough approximation for Anthropic's tokenizer.
The encoding is loaded lazily and cached at module level.

```python
client.count_tokens("Hello world")          # → small int

# Cost estimation at Sonnet pricing ($3/M input, $15/M output).
client.estimate_cost(prompt_tokens=1000, completion_tokens=500)   # USD
```

You can also import the helpers directly:

```python
from plinth import tokens

tokens.count("...")
tokens.estimate_cost(1_000_000, 500_000)
```

To update pricing, edit `plinth.tokens.SONNET_INPUT_USD_PER_MTOK` /
`SONNET_OUTPUT_USD_PER_MTOK` — they are exposed as named constants for
exactly this reason.

## Agent decorator

`@client.agent` wires a function into a workspace and exposes a tiny
`ctx` object with everything the agent needs:

```python
@client.agent(workspace="research-task-1", agent_id="agent-007")
def my_agent(ctx, topic: str):
    # ctx.workspace, ctx.tools, ctx.client are pre-bound.
    sources = ctx.tools.invoke("web.search", {"query": topic})
    for src in sources.result["results"]:
        page = ctx.tools.invoke("web.fetch", {"url": src["url"]})
        ctx.workspace.kv.set(f"sources/{src['url']}", page.result)
    return ctx.workspace.snapshot("done")

snapshot = my_agent(topic="renewable energy")
```

`ctx.tools.invoke(...)` automatically tags every call with the agent's
`workspace_id` and `agent_id` so the audit log lines up cleanly.

## Error handling

Every HTTP error from the workspace or gateway is mapped to a typed
exception. Each one carries `.code`, `.message`, `.details`, and the raw
`.response` (an `httpx.Response`) for advanced cases.

```python
from plinth import (
    PlinthError,                 # base class
    InvalidArguments,            # 400
    Unauthorized,                # 401
    NotFoundError,               # base for the 404 family
    WorkspaceNotFound,
    KeyNotFound,
    FileNotFound,
    SnapshotNotFound,
    BranchNotFound,
    ToolNotFound,
    ChannelNotFound,             # v0.2 — channel does not exist
    MessageNotFound,             # v0.2 — channel message gone
    WorkflowNotFound,            # v0.2 — workflow does not exist
    WorkflowStepNotFound,        # v0.2 — step ID does not exist
    InvalidWorkflowStep,         # v0.2 — step name not in manifest (400)
    RateLimited,                 # 429
    ToolInvocationError,         # tool was found but failed
)

try:
    ws.kv.get("missing")
except KeyNotFound as exc:
    print(exc.code)              # → "KEY_NOT_FOUND"
    print(exc.status_code)       # → 404
    print(exc.message)
    print(exc.details)
```

For the common "miss is fine" case, `ws.kv.get` accepts a `default`:

```python
topic = ws.kv.get("topic", default="renewable energy")
```

## Configuration reference

```python
Plynf(
    workspace_url="http://localhost:7421",
    gateway_url="http://localhost:7422",
    api_key="local-dev",
    timeout=30.0,
)
```

| Argument | Default | Notes |
|----------|---------|-------|
| `workspace_url` | `http://localhost:7421` | Workspace service base URL. |
| `gateway_url` | `http://localhost:7422` | Tool gateway base URL. |
| `api_key` | required | Sent as `Authorization: Bearer <key>`. |
| `timeout` | `30.0` | Per-request timeout in seconds. |
| `workspace_transport` | `None` | Optional `httpx.BaseTransport` (used for tests). |
| `gateway_transport` | `None` | Same, for the gateway client. |

The client supports the context-manager protocol so you can scope its
lifetime explicitly:

```python
with Plynf(api_key="local-dev") as client:
    ws = client.workspace("...")
    ...
```

## Development

```bash
cd sdk/python
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -v --cov=plinth --cov-report=term-missing
ruff check .
black --check .
```

The test suite uses [respx](https://lundberg.github.io/respx/) to mock both
services; no live HTTP calls are made.

## License

Apache-2.0 — see [LICENSE](../../LICENSE).
