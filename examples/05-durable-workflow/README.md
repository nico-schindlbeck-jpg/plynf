# Example 05 — Durable workflow execution

This example demonstrates the **v0.5 durable workflow executor**:
workflows run inside a separate worker process pool, with lease + heartbeat
coordination. If a worker dies mid-step, another worker takes over.

## What it shows

* **Worker death is recoverable.** Kill a worker mid-flight; the workspace's
  lease reaper expires its leases and reverts the in-flight step back to
  `pending`. A new worker picks it up.
* **Multiple workers can share work.** Run two `plinth-workflow-worker`
  processes in parallel; each picks different steps via the race-safe
  `acquire_lease` path.
* **Lease + heartbeat is the mechanism.** Workers acquire a lease (TTL = 60s
  by default) when they start a step, and heartbeat every 15s while running.
  If the heartbeat lapses, the reaper marks the lease `expired` and the
  step goes back to the pool.

## Architecture

```
┌────────────────┐       ┌──────────────────┐       ┌─────────────┐
│ start_workflow │──────▶│  workspace svc   │◀─lease│   worker A  │
│   (driver)     │       │   (port 7421)    │       │  (handlers) │
└────────────────┘       │                  │       └─────────────┘
                         │ workflow_step    │       ┌─────────────┐
                         │ workflow_lease   │◀─lease│   worker B  │
                         │ workers          │       │  (handlers) │
                         └──────────────────┘       └─────────────┘
                                  │                        │
                                  │  reaper (every 30s)    │
                                  └────────────────────────┘
                              expires stale leases →
                              reverts step pending
```

The driver `start_workflow.py` *creates* the workflow + steps but does
NOT execute them. Workers execute them.

## Prerequisites

```bash
# 1. Start the workspace + gateway services
make services

# 2. Install this example (editable, so the modules import correctly)
pip install -e ./examples/05-durable-workflow
```

## Walk-through

### Terminal 1 — start a worker

```bash
cd examples/05-durable-workflow
plinth-workflow-worker --handlers-module handlers --concurrency 2
```

The worker registers, finds no work, and idles. Logs every couple seconds.

### Terminal 2 — start a workflow

```bash
cd examples/05-durable-workflow
python start_workflow.py --topic "renewable energy"
```

The driver creates a workflow with four steps in `pending` status and polls
until it completes. In Terminal 1 you'll see the worker pick up each step,
heartbeat while running it, then release. The driver eventually prints
`report.md`.

### Crash + recovery demo

Run `start_workflow.py` again with a fresh topic. Mid-flight (after the
worker has logged `worker.lease.acquired` for at least one step), kill the
worker (`Ctrl+C` in Terminal 1).

Wait ~60 seconds for the reaper to expire the worker's leases (configurable
via `PLINTH_LEASE_REAPER_INTERVAL_SECONDS`). Then start a fresh worker:

```bash
plinth-workflow-worker --handlers-module handlers --concurrency 2
```

The new worker picks up the now-`pending` steps and finishes the workflow.
The driver, which is still polling, reports completion and prints the report.

### Multi-worker

Open two more terminals and run `plinth-workflow-worker` in each. Hit
the start script. Each step is leased by exactly one worker (the
race-safe acquire path), and you'll see different workers handle different
steps.

## How it works under the hood

1. `start_workflow.py` calls `wf.start_step(name, input=..., initial_status="pending")`
   for each manifest entry.
2. Workers poll `GET /v1/workspaces/.../workflows/.../pending`, filter by
   their registered `(workflow_name, step_name)` keys.
3. For each candidate, the worker `POST .../steps/{id}/lease` with its
   `worker_id` + a TTL. The workspace returns `200` (lease acquired) or
   `409 LEASE_CONFLICT` (someone else got it).
4. While running the handler, a per-lease heartbeat task `POST .../heartbeat`
   every `heartbeat_interval` seconds.
5. On success, the worker `POST .../release` with `status=completed` and
   the handler's return value as `output`.
6. On exception, the worker releases with `status=failed` and the
   exception's `str(exc)` as the error.
7. If the worker dies, the reaper expires its leases and reverts steps
   back to `pending`. Another worker picks up.

## Backwards compatibility

This example uses `initial_status="pending"` to opt into the durable
flow. Older v0.2 examples (e.g. `examples/03-resumable-workflow/`) use
the default `initial_status="running"` and continue to work unchanged —
the workspace's lease tables are additive.

## TypeScript variant

The same demo also runs against the Node worker (`@plinth/workflow-worker`).
Files are alongside the Python ones:

- `handlers.ts` — exports `register(runtime, client)` populating four
  step handlers (mirrors `handlers.py`).
- `start-workflow.ts` — driver that creates the workflow + waits
  (mirrors `start_workflow.py`).
- `shared.ts` — mock LLM / search / fetch helpers (mirrors `shared.py`).

### One-shot setup (compile both files)

The TS SDK and worker package live in this monorepo. A small
`tsconfig.json` is provided alongside the sources — point `tsc` at it
once the dependencies are built:

```bash
# Build the workspace dependencies once
(cd ../../sdk/typescript && npm install && npm run build)
(cd ../../worker-ts     && npm install && npm run build)

# Compile the example sources to ./build/*.js
cd examples/05-durable-workflow
npx tsc -p tsconfig.json
```

The provided tsconfig wires `@plinth/sdk` and `@plinth/workflow-worker`
to the local `dist/index.d.ts` files via `paths`, so the example
type-checks against the same artifacts you'd publish.

### Terminal 1 — start a TS worker

```bash
node ../../worker-ts/dist/cli.js \
  --handlers-module ./build/handlers.js \
  --concurrency 2
```

The CLI imports your compiled `handlers.js` and calls its `register(runtime, client)`
function on startup, then enters the same poll → lease → execute → release
loop as the Python worker.

### Terminal 2 — run the workflow

```bash
node ./build/start-workflow.js --topic "renewable energy"
```

The driver prints progress as the worker handles each step, then dumps
the synthesised report from `report.md`. Crash recovery, multi-worker,
and lease semantics all work identically — pick the language that suits
your stack.

## Files

- `handlers.py` / `handlers.ts` — handler registrations for the four
  steps. Imported by the matching worker (Python or Node) on startup.
- `start_workflow.py` / `start-workflow.ts` — drivers that create the
  workflow + wait.
- `shared.py` / `shared.ts` — mock LLM / search / fetch helpers so the
  demo runs offline.
- `reports/` — generated report files (gitkeep stub).
