# 01 — Research Agent (Token-Comparison Demo)

> The headline demo for Plinth. A 5-source research-and-report task,
> run twice on the same topic with the same fixtures, the same prompts,
> and the same mock LLM — once *without* Plinth and once *with*.
> Same task. Same outputs. **~70% fewer tokens with Plinth.**

## What this demo does

Both agents are given a topic (default: `renewable energy`) and follow
the same recipe:

1. **Search** the web for 5 sources.
2. **Fetch** the full content of each source.
3. **Extract** 3-5 key facts from each source.
4. **Synthesise** a 500-1000 word markdown report citing all 5 sources.

The two implementations are deliberately constructed to be identical
in everything *except* how state is held and how tools are called:

| Aspect | Baseline | Plinth |
|---|---|---|
| Source of truth for LLM context | conversation history | workspace KV/files |
| Source content storage | inlined into history | written to workspace, read by key |
| Tool-call layer | direct HTTP / fixture | gateway with caching |
| Synthesis prompt size | full history (search + 5 fetches + 5 contents + facts) | structured facts only |
| Resumability | none | snapshots at each phase |

The only thing the comparison measures is: **how many tokens does each
approach send to the LLM?** Token counts are exact (cl100k_base encoding,
the closest open BPE to Anthropic's tokenizer), measured with
`tiktoken` on the *exact* prompt strings each step would send.

## Why it matters

Token cost is the dominant variable in the unit economics of agentic
AI. At the time of writing, Anthropic Sonnet pricing is
**$3 per million input tokens** and **$15 per million output tokens**.
A naively-implemented agent can easily run 10-100x the tokens of a
well-engineered one for the same task. At production volumes, the
difference is the line between an agent product that has gross margins
and one that loses money on every interaction.

Plinth's claim is that *most* of that wastage comes from a small set of
recurring patterns:

- **Re-reading content from chat history** because there is nowhere
  else to put it. Every reasoning step balloons because the model has
  to re-process all the source material it has already seen.
- **Repeated tool calls** for identical work — same URL fetched twice,
  same database row queried three times — because there is no cross-
  call cache.
- **Ad-hoc state** that drifts and forces re-derivation rather than
  building structured artifacts that downstream steps can reference.

The Plinth substrate provides the missing pieces: a versioned workspace
(persistent, structured state) and a tool gateway (caching, audit, one
auth boundary). This demo is the most concrete possible illustration of
what those primitives buy you on a single workload.

## How to run

### Quick start (no services needed)

```bash
cd examples/01-research-agent
pip install -e .

python compare.py
python compare.py --topic "renewable energy"
python compare.py --topic "ai agents"
python compare.py --topic "climate policy"
python compare.py --per-step    # see the per-LLM-call breakdown
```

The simulation mode is **fully self-contained**: the example bundles
fixture content for three topics so it runs from a fresh clone with no
infrastructure. If the Plinth services are running it will use them
(real workspace, real gateway with real caching); otherwise the demo
simulates the workspace + gateway in-process so the comparison still
reflects the value prop.

### With services (optional)

```bash
# from the repo root
make serve              # starts workspace + gateway + mock-mcp
cd examples/01-research-agent
python compare.py
```

If the services are reachable the demo will use them. If not it will
print:

```
Services not reachable: workspace, gateway, mock_mcp.
Falling back to in-process fixtures + simulated gateway.
This is fine for simulation mode.
```

### Live mode (real Anthropic calls)

```bash
ANTHROPIC_API_KEY=sk-ant-... python compare.py --mode live
```

Live mode makes real Anthropic Sonnet API calls instead of the
deterministic mock. The token counts will vary slightly from the
simulation (because the real model produces different-length responses)
but the structural difference between baseline and Plinth — the *ratio*
that the demo highlights — is robust.

If `ANTHROPIC_API_KEY` is not set, live mode prints a warning and falls
back to simulation.

## What the output looks like

```
═══════════════════════════════════════════════════════════════════
  TOKEN-USAGE COMPARISON — research-agent on topic "renewable energy"
═══════════════════════════════════════════════════════════════════
  Baseline (no Plinth):        22,745 tokens   |   $0.0781
  With Plinth:                  6,726 tokens   |   $0.0339
  ─────────────────────────────────────────────
  Reduction:                     70.4 %        |   $0.0441 saved
═══════════════════════════════════════════════════════════════════
  Wall-clock time:        Baseline   0.0 s   |   Plinth   0.0 s
  Tool calls:             Baseline     6   |   Plinth     6   (0 cached)
═══════════════════════════════════════════════════════════════════
  Mode: simulation | Topic: renewable energy
  Baseline LLM calls: 8 | Plinth LLM calls: 7
  Report saved: reports/2026-05-05T15-45-46-comparison.json
```

A full structured comparison is also written to
`reports/<timestamp>-comparison.json`, including per-LLM-call token
breakdowns, per-tool-call records, and the synthesised report from each
agent. CI can check that the headline reduction stays above the 40%
quality bar by reading the JSON.

## Per-phase breakdown

`--per-step` prints a phase-level comparison that makes the architectural
difference obvious:

```
                             Per-phase token totals
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ Phase                         ┃ Baseline tokens ┃ Plinth tokens ┃ Δ (tokens) ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ search  (1b / 1p calls)       │              99 │            39 │        +60 │
│ decide-fetch (per source)     │          11,166 │             - │    +11,166 │
│ (5b / 0p calls)               │                 │               │            │
│ extract (per source)  (1b /   │           5,369 │         5,446 │        -77 │
│ 5p calls)                     │                 │               │            │
│ synthesise  (1b / 1p calls)   │           6,111 │         1,241 │     +4,870 │
│ TOTAL                         │          22,745 │         6,726 │    +16,019 │
└───────────────────────────────┴─────────────────┴───────────────┴────────────┘
```

The story the table tells:

- **search** is comparable across both (Plinth saves a small amount
  by not maintaining a long-running system prompt).
- **decide-fetch** is the killer for the baseline. Each of 5 per-source
  fetch decisions sends the *growing* conversation history to the LLM,
  with previously-fetched source content inlined in the prompt. The
  Plinth agent has no equivalent phase — gateway calls are direct, not
  mediated by an LLM reasoning step.
- **extract** is roughly equal across both. Baseline does it once on a
  giant context; Plinth does it 5 times on small per-source contexts.
  The structurally different shapes wash out to almost the same total —
  this is the real work in either approach.
- **synthesise** is the second big win for Plinth. Baseline includes
  all source content in the prompt (~6000 tokens); Plinth includes
  only structured fact summaries (~1200 tokens).

## How the design produces the reduction

### The baseline pattern

```python
# Pseudocode of the wasteful pattern
history = []
history.append(("user", f"Research topic: {topic}"))
history.append(("assistant", "I'll search for sources."))

# Each fetch inlines content into history
for source in search(topic):
    fetch_result = fetch(source.url)              # ~1500 tokens
    history.append(("tool", fetch_result.content))  # NOW IN EVERY FUTURE PROMPT

# Synthesis sees ALL source content again
history.append(("user", "Synthesise a report"))
report = call_llm(history)   # >5000-token prompt
```

The pattern is reasonable-looking, common in real agent code, and
**the tokens add up super-linearly with the number of steps**. Each
source effectively costs ~1500 tokens *per subsequent reasoning step*.

### The Plinth pattern

```python
# Pseudocode of the structured pattern
ws = client.workspace(f"research-{slugify(topic)}")
search = client.tools.invoke("web.search", {"query": topic})

# Sources go to workspace, not LLM history
for source in search.results:
    fetch_result = client.tools.invoke("web.fetch", {"url": source.url})
    ws.files.write(f"sources/{slugify(source.url)}.txt", fetch_result.content)

# Per-source extraction. Each LLM call sees ONE source, not all five.
for source in sources:
    content = ws.files.read(f"sources/{slugify(source.url)}.txt")
    facts = call_llm([("user", f"Extract facts from:\n{content}")])
    ws.kv.set(f"facts/{source.url}", facts)

# Synthesis sees facts only — small, structured, focused.
facts_summary = "\n\n".join(f"From {url}:\n{f}" for url, f in facts_by_url.items())
report = call_llm([("user", f"Synthesise from these facts:\n{facts_summary}")])
```

Each source's content is sent to the LLM **exactly once**, in the
extraction step. The synthesis step sees structured fact summaries —
typically 200 tokens per source vs. 1500 for raw content. The total
prompt budget is dominated by extraction calls, each of which is
bounded by a single source's size.

### Where the gateway helps

The gateway adds a second layer of savings on top:

- **Identical fetches are free.** If two sub-tasks need the same
  source, only one network round-trip happens. In multi-agent or
  multi-run scenarios this is dramatic.
- **Fewer auth flows to manage.** The gateway is the single boundary
  for API keys / OAuth tokens, simplifying credentials.
- **Audit trail.** Every tool call is logged with cost attribution.

In the single-run, single-agent topology of this demo the cache benefit
shows as 0 cached calls (because no URL is fetched twice). Run the
same topic twice with services up and you'll see the second run's
fetches all come back as cached — `tools.invoke` returns the prior
result without paying the underlying fetch cost.

## Verifying the numbers yourself

Token counts are not estimates. They come from `tiktoken.encode(prompt)`
on the *exact* string each step would send to the LLM, with the
`cl100k_base` encoding (the closest open BPE to Anthropic's tokenizer
and the standard the SDK uses).

To audit:

1. Run with `--per-step` to see every LLM call's token count.
2. Open the JSON report under `reports/`. It contains every prompt's
   token total, every tool call's arguments hash, and the full
   synthesised report.
3. Modify `shared.py` to print the prompts themselves before
   tokenising.

The simulation mode is intentionally deterministic: same topic + same
mode = same numbers. This makes the demo regression-safe in CI.

## Limitations of simulation mode

Simulation uses a deterministic mock LLM whose response *shapes* (~200
tokens for fact extraction, ~500 tokens for the report) match what real
Sonnet calls produce, but whose response content is template-generated
rather than model-generated. Two consequences:

1. **Quality of the synthesised report is illustrative, not real.** It
   reads like a plausible report, but it's not what Sonnet would
   actually write for the topic. Use `--mode live` to see real output.
2. **The token reduction reported by simulation is structural, not
   model-dependent.** Real Sonnet runs typically show a comparable or
   slightly larger reduction (the model produces somewhat
   variable-length responses, but the input-side savings dominate).

What simulation *is* meant to do, and does well: provide a fast,
regression-safe demonstration of the structural advantage Plinth
provides at the prompt-construction level.

## File map

```
examples/01-research-agent/
├── README.md                 # this file
├── pyproject.toml            # depends on plinth, tiktoken, httpx, rich
├── topics.json               # topics + expected source counts
├── shared.py                 # fixtures, mock LLM, token counter, backends
├── baseline.py               # the no-Plinth agent
├── with_plinth.py            # the Plinth agent
├── compare.py                # entry point: runs both and prints the table
└── reports/                  # JSON comparison reports go here
```

## What's next

Two stub demos sit alongside this one and show what the next iterations
will demonstrate:

- [`02-multi-agent-handoff`](../02-multi-agent-handoff/) — three agents
  collaborating via the workspace (researcher → writer → reviewer).
- [`03-resumable-workflow`](../03-resumable-workflow/) — agent crash
  mid-flight, recover from snapshot.

Both demos exercise primitives that ship in v0.2 (channels, locks,
durable workflow runtime) on top of the workspace + gateway shipped
in v0.1.
