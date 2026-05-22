# Workflow Benchmark Suite — Methodology

How we measure the token-cost difference between agent workflows running on Plynf versus a naive chat-history-only baseline. Designed to be reproducible, falsifiable, and honest about its limits.

## What we measure

For each workflow scenario, the suite computes:

| Metric | Definition |
|---|---|
| `baseline_input_tokens` | Sum of all input tokens sent to the model across all steps in the naive (chat-history) implementation |
| `baseline_output_tokens` | Sum of all output tokens produced by the model |
| `plinth_input_tokens` | Same, but for the Plynf implementation that uses handles, snapshots, and channels |
| `plinth_output_tokens` | Same |
| `reduction_pct` | `1 - (plinth_total / baseline_total)`, rounded to one decimal place |
| `cost_baseline_usd` | Priced at Claude Sonnet 4.5 list: $3/M input, $15/M output (May 2026) |
| `cost_plinth_usd` | Same pricing applied to the Plynf-side tokens |
| `cost_saved_usd` | `cost_baseline - cost_plinth` |

The suite is **deterministic**: no real LLM calls. Each scenario specifies its step structure as data; the harness computes the token math from that structure. This makes the numbers reproducible in CI and immune to model-side variance.

## Why we DON'T call a real LLM

Three reasons:

1. **Reproducibility.** Real LLM outputs vary run-to-run. Quoting a number like "71%" on the marketing site requires a measurement that's stable across runs and reviewable by a stranger with a clean clone.
2. **Apples to apples.** The comparison is about input-token routing, not model quality. If the model in question is held constant, the *only* variable is what the prompt looks like at each step — that's what we measure.
3. **Cost.** Running 8 scenarios × N variations × paid LLM = expensive for a CI job. Mock mode is $0.

For users who want a real-LLM smoke test, `examples/06-llm-research-agent` runs against a live Anthropic API. The numbers it produces are within ±5% of the deterministic suite per our cross-check.

## The token model

Each scenario is a list of steps. Each step has:

| Field | Meaning |
|---|---|
| `id` | Stable name (`fetch_source_1`, `summarise_pdf_3`, etc.) |
| `kind` | `tool_call`, `model_call`, or `handoff` |
| `produces_tokens` | Output size (model response, tool result, or message content) |
| `requires` | List of prior step IDs whose **outputs** this step needs |
| `prompt_overhead` | Fixed tokens for the step's instruction/template |

### Baseline accounting

The naive implementation re-sends every prior step's output with the current step's prompt:

```python
baseline_input(step_i) = prompt_overhead(step_i) + sum(produces_tokens(step_j) for j < i)
```

This is the "chat history" model — every model call sees the entire prior conversation. It's how LangChain's default agent loop, vanilla OpenAI function calling, and most LangGraph configurations work today.

### Plynf accounting

The Plynf implementation:
- Tool calls write outputs to the workspace; return a **handle** to the agent (~30 tokens).
- Model calls require **only the handles to the prior outputs they need**, plus a small `read_size_tokens` budget per handle for selective dereference.
- Multi-agent handoffs send messages on **typed channels**; each agent reads only what's addressed to it.

```python
plinth_input(step_i) = (
    prompt_overhead(step_i)
    + sum(HANDLE_TOKENS for j in step_i.requires)
    + sum(read_size_tokens(j) for j in step_i.requires)
)
```

Where `HANDLE_TOKENS = 30` (the URI itself) and `read_size_tokens` defaults to 300 (a summary) or 1500 (a focused slice of the full content), specified per step.

## Constants used by the harness

```python
HANDLE_TOKENS         = 30      # "ws://my-research/sources/foo.txt@v17"
DEFAULT_SUMMARY_SIZE  = 300     # Auto-generated summary, projection
DEFAULT_SLICE_SIZE    = 1500    # Focused dereference (search top-k, named section)
CLAUDE_SONNET_INPUT   = 3.00    # $/M tokens, May 2026 list
CLAUDE_SONNET_OUTPUT  = 15.00   # $/M tokens
```

Output tokens are equal between baseline and Plynf — the model produces the same response whatever you put in the prompt; we're not measuring quality differences. The savings come exclusively from the input side, but we report total (input + output) for honesty.

## What the suite is NOT measuring

- **Wall-clock latency.** Plynf adds a small overhead for workspace I/O (~20-50ms per write). For workflows of 5+ steps, the latency wins on Plynf's side because parallel branches reduce serial chain length. The suite doesn't claim a latency number; we have separate `bench` runs for that.
- **Output quality.** Two prompts that differ in structure but contain the same information should produce equivalent quality. Anecdotally this holds for the workflows we've measured; rigorously, we'd need eval grading and that's a separate workstream.
- **Cache hits.** The tool gateway caches responses, which makes the second run of any workflow much cheaper. The suite assumes a cold cache for both baseline and Plynf — the comparison is on first-run tokens.
- **Streaming output.** Both implementations stream the same way. Not a differentiator.

## Sanity checks

Two safeguards against the suite cooking the numbers:

1. **Lower-bound floor.** No scenario reports >85% reduction without an explicit reason in the scenario file. If a workflow shows 90%+ savings, either the baseline is degenerate or the scenario is unrealistic. The harness emits a warning that the maintainer must justify.
2. **Cross-check with examples/.** The 5-source research scenario in `scenarios/research_5_source.py` must produce within ±3 percentage points of the live `examples/01-research-agent/compare.py` output. CI fails if they diverge.

## Reading the output

```bash
python -m benchmarks.workflows.run_all
```

Produces a Rich-formatted comparison table on stdout and a JSON report at `benchmarks/workflows/results/<timestamp>-suite.json`. Each scenario gets its own row showing baseline vs Plynf tokens, reduction percentage, and cost-saved dollars.

The JSON report is the canonical artefact for citing in marketing copy, sales decks, or technical write-ups. Always link to a specific run, not the latest, so the numbers you quote can be reproduced.
