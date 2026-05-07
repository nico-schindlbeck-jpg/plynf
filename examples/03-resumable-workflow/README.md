# 03 — Resumable Workflow Demo

> An agent crashes mid-flight. A new process starts. It picks up
> exactly where the crashed one left off — without redoing completed
> work, without losing in-progress state.
>
> **This is what makes long-running agents viable in production.**

## What this demo does

A 6-step "deep research" pipeline runs on a topic:

| # | Step       | What it does                                         | LLM cost (approx) |
|---|------------|------------------------------------------------------|-------------------|
| 1 | `discover` | mock `web.search` → 5 sources, write to KV           | ~50 tokens        |
| 2 | `fetch`    | mock `web.fetch` per source → write content to files | 0 (no LLM)        |
| 3 | `extract`  | per-source LLM extraction (5 calls)                  | ~7,500 tokens     |
| 4 | `outline`  | LLM produces a structured outline                    | ~2,000 tokens     |
| 5 | `write`    | LLM writes a long-form report                        | ~5,000 tokens     |
| 6 | `polish`   | LLM produces a polished/finalised version            | ~3,000 tokens     |

After **every** step the agent calls `ws.snapshot(...)` and records
the snapshot id on the workflow's step log. The snapshot is metadata-
only (a map of `{key: version, file_path: version}`), so taking one is
O(1) — cheap enough to do every step.

The demo runs `workflow_agent.py` twice as a subprocess on the same
workspace name:

* **Run 1** is invoked with `--crash-at outline`. It does the
  discover/fetch/extract steps cleanly (snapshot + complete each), then
  starts `outline`, does the LLM call, and **deliberately exits 99
  before snapshotting**. The workflow on disk is left with three steps
  completed and one step running.

* **Run 2** is invoked on the same workspace with no `--crash-at`. The
  agent calls `wf.resume_info()`, finds the next pending step is
  `outline`, and walks the manifest from there. Discover / fetch /
  extract are not redone — the agent reads them from the workspace KV
  and files.

Between the runs the demo prints the workflow state, so you can see
what was preserved across the crash boundary.

## How to run

### Quick start (no services needed)

```bash
cd examples/03-resumable-workflow
pip install -e .

python crash_resume.py --topic "renewable energy"
python crash_resume.py --topic "ai agents"
python crash_resume.py --crash-at write       # crash at a different step
```

The simulation mode is **fully self-contained**: state persists to
`/tmp/plinth-data/03-resumable-workflow/` as JSON files, so the crash
+ resume sequence still demonstrates the value even without any
infrastructure. The crash is a real `sys.exit(99)`; the parent process
observes the return code and the resume agent is a fresh Python
process with no in-memory state from run 1.

### With services (preferred)

```bash
# from the repo root
make serve              # starts workspace + gateway + mock-mcp
cd examples/03-resumable-workflow
python crash_resume.py --topic "renewable energy"
```

When the workspace service is reachable the agent uses the real Plinth
SDK: `ws.workflows.create(...)`, `ws.snapshot(...)`,
`wf.resume_info()`, `wf.complete_step(...)`. The crash + resume
narrative is identical; the difference is that the workflow state and
snapshots live in the workspace service's SQLite, not in a JSON file.

### Inspect a workspace

```bash
python crash_resume.py --inspect <workspace-name>
```

Prints the workflow's manifest with each step's status and snapshot id.
Useful for debugging or for checking what an interrupted workflow looks
like before kicking off a resume.

### Direct agent invocations

The driver `crash_resume.py` invokes `workflow_agent.py` as a
subprocess. You can also run the agent directly:

```bash
# Crash mode
python workflow_agent.py --workspace my-ws --topic "..." --crash-at outline

# Resume (or fresh run if the workspace is new)
python workflow_agent.py --workspace my-ws --topic "..."
```

## What the output looks like

```
═══════════════════════════════════════════════════════════════════
  RESUMABLE WORKFLOW DEMO — topic: 'renewable energy'
═══════════════════════════════════════════════════════════════════
  Workspace: resume-demo-renewable-energy-a1b2c3d4e5

═══════════════════════════════════════════════════════════════════
  ▶ Run 1 — will crash at step 'outline'
═══════════════════════════════════════════════════════════════════
  [agent] starting fresh at step 'discover'
  [agent] step 'discover' complete (snap=snap_..., +91 tokens)
  [agent] step 'fetch' complete (snap=snap_..., +0 tokens)
  [agent] step 'extract' complete (snap=snap_..., +7,540 tokens)
  [crash] simulating crash before completing step 'outline'
  [agent] CRASH at step 'outline' (work done but not committed; +5,041 tokens lost)
  → exit code 99

  Workflow state after crash:
  Workflow: deep-research (wf_...)
  Status:   running
  Steps:
    ✓ discover   completed at 17:42:13  (snapshot: snap_...)
    ✓ fetch      completed at 17:42:13  (snapshot: snap_...)
    ✓ extract    completed at 17:42:13  (snapshot: snap_...)
    ◐ outline    running    (no snapshot — incomplete)
    · write      pending
    · polish     pending

═══════════════════════════════════════════════════════════════════
  ▶ Run 2 — resume from snapshot
═══════════════════════════════════════════════════════════════════
  [agent] resuming from snapshot snap_... at step 'outline'
  [agent] step 'outline' complete (snap=snap_..., +5,041 tokens)
  [agent] step 'write' complete  (snap=snap_..., +14,019 tokens)
  [agent] step 'polish' complete (snap=snap_..., +5,824 tokens)
  → exit code 0

  Final workflow state:
  Workflow: deep-research (wf_...)
  Status:   completed
  Steps:
    ✓ discover   completed
    ✓ fetch      completed
    ✓ extract    completed
    ✓ outline    completed   ← redone successfully
    ✓ write      completed
    ✓ polish     completed

═══════════════════════════════════════════════════════════════════
  RESUME COMPLETE
═══════════════════════════════════════════════════════════════════
  Run 1 (crashed at 'outline'):  12,672 tokens   |   $0.0...
  Run 2 (resumed):               24,884 tokens   |   $0.0...
  ─────────────────────────────────────────────
  Saved by resume:               12,672 tokens
═══════════════════════════════════════════════════════════════════
  Without Plinth's resume: every crash means starting over from scratch.
  With Plinth: ~34% of work avoided on resume.
═══════════════════════════════════════════════════════════════════
```

A full structured report is written to `reports/<timestamp>-resumable-<topic>.json`.

## What this demo proves

### 1. Checkpointing is cheap

Snapshots are O(metadata): a `Snapshot` record is just a dict of
`{key: version, path: version}` plus an id. Taking one after every
step costs effectively nothing. There's no reason **not** to snapshot
at every checkpoint, even for steps that look mechanical.

### 2. Resume is automatic

The agent doesn't have to remember anything across the crash. It
asks the workflow:

```python
info = wf.resume_info()
# info.next_step → "outline"
# info.snapshot_id → snap_<ulid>  (the most recent completed step's snapshot)
```

That's it. The resume code path is identical to the cold-start path —
the only thing that changes is which step the loop starts at.

### 3. State is durable

Workspace KV + files survive process crashes. Snapshots make
**exact** restoration possible: every snapshot pins a specific
version of every key/file. After a crash the resuming agent reads
the workspace as of the most recent snapshot. The "running" step
that the crashed process started but didn't snapshot is simply
re-attempted from the prior snapshot's state.

### 4. Cost savings are real

In a 6-step pipeline, a crash on step 4 with no resume support means
redoing the 3 prior steps' tokens. That's 80%+ wasted work in the
extract-heavy pipeline this demo runs. With Plinth's snapshots: 0%
wasted work on resume.

### 5. Production reliability story

This is the story that makes long-running agents viable. Without
durable state and snapshot-based resume, every long-running agent
either:

- Re-runs from scratch on every failure (cost-prohibitive for any
  workflow over a couple of minutes).
- Implements its own bespoke crash-recovery (and inevitably gets it
  wrong in subtle ways).

Plinth's workspace + workflows API is the missing primitive: a
ready-made "checkpoint here, resume from here" abstraction that any
agent can opt into for free.

## How the agent is structured

The 6-step loop is a straightforward walk through the manifest:

```python
wf = ws.workflows.get_or_create("deep-research", steps=WORKFLOW_STEPS)
info = wf.resume_info()

if info.next_step is None:
    return  # already complete

# Walk the manifest from the next pending step.
remaining = wf.steps_manifest[wf.steps_manifest.index(info.next_step):]
for step_name in remaining:
    step = wf.start_step(step_name)
    output = do_step_work(ws, step_name, topic)
    snap = ws.snapshot(f"after-{step_name}")
    wf.complete_step(step.id, output=output, snapshot_id=snap.id)
```

The deliberate crash trigger is a check at the top of each iteration:

```python
if step_name == crash_at:
    wf.start_step(step_name)        # mark running (so the resume sees it)
    do_step_work(ws, step_name, topic)
    raise SystemExit(99)            # exit before snapshot + complete
```

That's the entire mechanism. The reason this is a useful primitive is
that any production agent needs roughly this code path; offloading it
to the substrate means writing it once and getting it right.

## File map

```
examples/03-resumable-workflow/
├── README.md            # this file
├── pyproject.toml       # depends on plinth, tiktoken, httpx, rich
├── shared.py            # mock LLM, fixtures, simulated stores, token counter
├── workflow_agent.py    # the agent: 6-step loop with checkpoints
├── crash_resume.py      # demo entry point: runs agent twice, prints comparison
└── reports/             # JSON reports go here
```

## Dependencies on Plinth APIs

The agent uses the following SDK surfaces (all defined in
[`CONTRACTS.md`](../../CONTRACTS.md)):

* `client.workspace(name)` — get-or-create workspace.
* `ws.kv.set(key, value)` / `ws.kv.get(key)` — versioned KV.
* `ws.files.write(path, content)` / `ws.files.read(path)` — versioned files.
* `ws.snapshot(name)` — point-in-time snapshot.
* `ws.workflows.create(name, steps=[...])` — create a workflow.
* `wf.start_step(name)` — start a step (transitions to `running`).
* `wf.complete_step(step_id, output, snapshot_id)` — mark complete.
* `wf.resume_info()` — query the next pending step + snapshot.

When the services aren't reachable the example uses an in-process
file-backed simulation that has the same observable semantics. The
demo's reliability story is identical in either mode.

## What's next

This demo establishes the resumable-workflow primitive on a single
agent. The natural follow-on is multi-agent resumable pipelines —
cross-agent handoff via channels combined with workflow checkpointing
gives you durable, restartable, multi-agent workflows.

See [`02-multi-agent-handoff`](../02-multi-agent-handoff/) for the
multi-agent piece.
