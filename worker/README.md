# plinth-workflow-worker

Durable workflow worker for the Plinth platform.

The worker process polls the Plinth workspace service for pending workflow steps,
acquires a lease, dispatches to a registered handler, then releases the lease.
On crash, the workspace's lease reaper expires the worker's leases and reverts
the steps back to `pending` so another worker can take over.

## Install

```bash
pip install plinth-workflow-worker
```

## Run

```bash
plinth-workflow-worker \
  --workspace-url http://localhost:7421 \
  --gateway-url http://localhost:7422 \
  --api-key local-dev \
  --concurrency 4 \
  --lease-ttl 60 \
  --heartbeat-interval 15 \
  --handlers-module myapp.handlers
```

`--handlers-module` is the dotted Python module path. The worker imports it
on startup, expecting it to register handlers via:

```python
from plinth import Plinth

client = Plinth(workspace_url=..., gateway_url=..., api_key=...)

@client.workflow_handler("research-pipeline", step="search")
def search_step(ctx):
    topic = ctx.step.input["topic"]
    return ctx.tools.invoke("web.search", {"query": topic, "k": 5})
```

The worker reuses the `Plinth` instance defined in your handlers module so the
registry, HTTP clients, and tool gateway all match what your handlers expect.

## Settings

All flags can also be set via env variables (`PLINTH_WORKSPACE_URL`,
`PLINTH_API_KEY`, `PLINTH_CONCURRENCY`, etc.). CLI overrides env.

| Flag | Env var | Default | Notes |
|------|---------|---------|-------|
| `--workspace-url` | `PLINTH_WORKSPACE_URL` | `http://localhost:7421` | |
| `--gateway-url` | `PLINTH_GATEWAY_URL` | `http://localhost:7422` | |
| `--identity-url` | `PLINTH_IDENTITY_URL` | (none) | Optional. |
| `--api-key` | `PLINTH_API_KEY` | `local-dev` | Bearer token. |
| `--concurrency` | `PLINTH_CONCURRENCY` | 4 | In-flight steps. |
| `--lease-ttl` | `PLINTH_LEASE_TTL` | 60 | Seconds. |
| `--heartbeat-interval` | `PLINTH_HEARTBEAT_INTERVAL` | 15 | Seconds. |
| `--handlers-module` | `PLINTH_HANDLERS_MODULE` | — | Required. |
| `--workspace` | — | (all) | Whitelist (can repeat). |

## Crash recovery

The worker process is durable in two ways:

1. **Worker-level**: A worker registers + heartbeats. If the worker crashes
   (no heartbeat for `worker_inactive_timeout_seconds`), the workspace's
   reaper marks it `gone`.
2. **Lease-level**: While running a step, the worker heartbeats the lease
   every `heartbeat-interval` seconds. If the worker dies mid-step, the
   reaper sweeps the expired lease and reverts the step from `running`
   back to `pending`. Another worker (or this one after restart) sees
   it as a candidate and takes over.

The step's *workspace state* — KV writes, file writes, snapshots — is
preserved by the workspace service, so the next worker resumes from
the most recent committed state.

## Containerised

```bash
docker run \
  -e PLINTH_WORKSPACE_URL=http://workspace:7421 \
  -e PLINTH_GATEWAY_URL=http://gateway:7422 \
  -e PLINTH_API_KEY=... \
  -e PLINTH_HANDLERS_MODULE=myapp.handlers \
  plinth-workflow-worker:0.5.0
```

A simple Dockerfile is provided in this directory for reference.
