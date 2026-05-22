# 02 — Multi-Agent Handoff Demo

> Three agents — **Researcher → Writer → Reviewer** — collaborate on a
> single research-and-write task, with structured handoffs through
> Plynf's **Channels** primitive. Same workspace, three roles, one
> auditable pipeline.

## What this demo does

A topic comes in (e.g. `"renewable energy"`); a finished, reviewed
report comes out. In between, three agents run as independent threads,
communicate only via typed channel messages, and never share LLM
context with each other.

```
                ┌────────────┐  research-out  ┌────────┐  writer-out  ┌──────────┐  final-out  ┌────────┐
   topic ──────▶│ Researcher │ ──────────────▶│ Writer │─────────────▶│ Reviewer │────────────▶│  done  │
                └────────────┘                 └────────┘               └──────────┘             └────────┘
                       │                            │                        │
                       ▼                            ▼                        ▼
                  ws.kv +                     ws.files                  ws.files
                  ws.files                    (draft.md)                (final.md,
                  (sources/, facts.json)                                 critique.md)
                       │                            │                        │
                       ▼                            ▼                        ▼
                  snapshot                   snapshot                  snapshot
                "research-complete"          "draft-complete"          "final-complete"
```

The Researcher gathers 5 sources, extracts structured facts per source,
snapshots the workspace, and emits a `research.complete` message
carrying the snapshot id and KV-key references. The Writer reads only
the small fact bullets (not the raw sources!) and produces a draft.
The Reviewer reads only the draft, critiques it, then produces the
final report.

Each agent is small, single-purpose, and **horizontally composable** —
add a fact-checker between Writer and Reviewer without touching either.

## What this demo proves

1. **Structured handoffs.** Agent A sends a typed message to agent B;
   B receives only the snapshot + key-references it needs, **not** the
   raw content. The writer's prompt is bounded by the size of the fact
   summaries, not the corpus size.
2. **Persistence.** Channels and workspace state are durable. If any
   agent crashes mid-flight, restart it and it picks up — the upstream
   message is still in the channel, the upstream snapshot is still in
   the workspace.
3. **Auditability.** Every step's snapshot is recorded
   (`research-complete`, `draft-complete`, `final-complete`). The full
   pipeline can be replayed from any of them.
4. **Composability.** You can swap, add, or remove an agent without
   changing any other agent. Each only knows about its inbound and
   outbound channels.

## How to run

### Quick start (with services)

```bash
# from the repo root
make serve              # starts workspace + gateway + mock-mcp
.venv/bin/pip install --ignore-requires-python -e ./examples/02-multi-agent-handoff

.venv/bin/python examples/02-multi-agent-handoff/orchestrate.py --topic "renewable energy"
```

### Quick start (no services — graceful fallback)

The demo also runs end-to-end with **no infrastructure** by falling
back to an in-process workspace + in-memory channel bus. The
architectural story is the same; only the persistence is gone.

```bash
cd examples/02-multi-agent-handoff
pip install -e .
python orchestrate.py --topic "renewable energy"
```

If services are reachable, you get real workspace HTTP, real persisted
channels, real snapshots in SQLite. If not, you get an in-process
simulation with the exact same `ws.kv` / `ws.files` / `ws.channels`
surface — same code, same agents, same results.

### Live mode (real Anthropic calls)

```bash
ANTHROPIC_API_KEY=sk-ant-... python orchestrate.py --topic "renewable energy" --mode live
```

The mock LLM is replaced with real Sonnet calls. Token counts will
vary per run; structural behaviour (handoffs, snapshots, channel flow)
is identical.

### Standalone agents

Each agent is also runnable directly. This is the deployment-style
form: agents could be on different hosts, talking through the
workspace's HTTP-backed channels.

```bash
# In one shell:
python researcher.py --workspace pipeline-renewable-energy-1 --topic "renewable energy"

# In another shell (after researcher finishes — sequential is most reliable):
python writer.py --workspace pipeline-renewable-energy-1

# And in a third:
python reviewer.py --workspace pipeline-renewable-energy-1
```

The orchestrator (above) runs the agents concurrently in threads and
shares one workspace handle. Run them sequentially when invoking the
standalone CLIs because the SDK's `workspace(name)` is get-or-create —
two parallel processes naming the same not-yet-existing workspace will
each create one, and the messages won't reach the right one.

## Output

```
═══════════════════════════════════════════════════════════════════
  PLYNF MULTI-AGENT PIPELINE — topic: 'renewable energy'
═══════════════════════════════════════════════════════════════════
  Workspace : pipeline-renewable-energy-17FE58AFC719
  Workspace ID: ws_01KQY17P2RYPHH60PZ5VAW056Q
  Backend   : sdk  (services: {'workspace': True, 'gateway': True, 'mock_mcp': True})

  ▶ researcher started   (tid 0x..., slot 22000)
  ▶ writer     started   (tid 0x..., slot 22001)
  ▶ reviewer   started   (tid 0x..., slot 22002)

═══════════════════════════════════════════════════════════════════
  PIPELINE RESULT — topic: 'renewable energy'
═══════════════════════════════════════════════════════════════════
  Workspace : pipeline-renewable-energy-17FE58AFC719
  Backend   : sdk

  Channel handoffs:
    research-out  : 1 message   (researcher → writer)
    writer-out    : 1 message   (writer → reviewer)
    final-out     : 1 message   (reviewer → done)

  Pipeline complete in 0.47s

  Tokens used:
    researcher :   5,514
    writer     :   1,187
    reviewer   :   2,146
    ────────────────────────
    TOTAL      :   8,847   |   $0.0493

  Final report : 547 words  (3826 chars)

  Snapshot history:
    snap_01KQY17P8XRB01KB5Y0  research-complete      (researcher)
    snap_01KQY17PAX4Y1TXST15  draft-complete         (writer)
    snap_01KQY17PH3ER59N6HBK  final-complete         (reviewer)
═══════════════════════════════════════════════════════════════════

  JSON report saved: reports/2026-05-06T06-57-32-pipeline.json
```

A full structured pipeline report is also written to
`reports/<timestamp>-pipeline.json` containing per-agent token
breakdowns, channel-message counts, snapshot ids, and the full final
report. CI can read this to verify the pipeline ran correctly.

## File layout

```
examples/02-multi-agent-handoff/
├── README.md          # this file
├── pyproject.toml     # depends on plinth, tiktoken, httpx, rich
├── topics.json        # default topic + bundled fixtures
├── shared.py          # mock LLM, prompt templates, run-record dataclasses,
│                      # in-process workspace+channels fallback
├── researcher.py      # Agent 1 — gather sources, extract facts
├── writer.py          # Agent 2 — synthesize draft from facts
├── reviewer.py        # Agent 3 — critique + finalize
├── orchestrate.py     # entry point: spawns 3 agents, runs full pipeline
└── reports/           # JSON pipeline reports
```

## Architectural deep-dive

### The handoff payload

The Researcher's `research-out` message looks like:

```json
{
  "topic": "renewable energy",
  "snapshot_id": "snap_01HKP...",
  "fact_keys": [
    "facts/mock://renewable-energy-1",
    "facts/mock://renewable-energy-2",
    "facts/mock://renewable-energy-3",
    "facts/mock://renewable-energy-4",
    "facts/mock://renewable-energy-5"
  ],
  "source_count": 5,
  "sources": [...]
}
```

Notice what's **not** in the message: the raw source content
(thousands of tokens) and the extracted facts themselves. The Writer
pulls the small facts by key from `ws.kv` — bounded, predictable cost.
The raw sources sit in `ws.files` for any agent that needs them, but
the typical pipeline never reads them again after extraction.

### The Channels API

The agents use `ws.channels.send(...)` and `ws.channels.receive(...)`
on the SDK's `ChannelsProxy`. Behind the scenes it's an HTTP POST/GET
to the workspace service. Channels are workspace-scoped, persistent,
and have monotonic per-channel sequence numbers. The full contract is
in [`/CONTRACTS.md`](../../CONTRACTS.md#channels-api-workspace-service).

The shared `wait_for_channel()` helper in `shared.py` does timeout-
bounded polling and treats the SDK's `ChannelNotFound` (raised when a
channel hasn't been created yet) as "no messages, keep waiting."

### Graceful fallback

`shared.get_pipeline_facade(workspace_name)` returns one of two
implementations:

* **SDK path** — when workspace + gateway + mock-mcp are all reachable.
  The agents talk to a real `plinth.Workspace` whose `kv` / `files` /
  `channels` calls go over HTTP. The pipeline state is durable across
  restarts.
* **Simulated path** — when any service is missing. Returns an
  `InProcessWorkspace` with the same surface, backed by Python dicts.
  The orchestrator's threads share the object, so handoffs still flow.
  Persistence is gone (Python process exit = state lost), but the
  demo of "structured handoffs across agents" is intact.

The agents themselves are entirely indifferent to which backend they
got. They call `facade.workspace.channels.send(...)` and that just
works.

### Comparing to a single-agent pipeline

A naive single-agent agent would have had:

* All five sources inlined into the synthesis prompt (~10,000 tokens).
* A single LLM context that grew unboundedly with each step.
* No checkpoint to resume from on crash.
* No way to split work across heterogeneous compute (small fast model
  for extraction, big slow model for finalisation).

This demo's three-agent split fixes all of those, without spending
more LLM tokens overall. The
[01-research-agent](../01-research-agent/) demo quantifies the
savings vs. a baseline single-agent pipeline; this demo goes further
and shows the multi-agent organisational pattern that becomes
practical once you have the right substrate.

## Verifying

```bash
# from the repo root
bash scripts/healthcheck.sh
.venv/bin/python examples/02-multi-agent-handoff/orchestrate.py --topic "renewable energy"
# inspect the produced report
ls -1 examples/02-multi-agent-handoff/reports/
```

The pipeline is deterministic in simulation mode: same topic, same
results. Tools track tokens via `tiktoken.encode` so the JSON reports
are exact — no hidden cost.
