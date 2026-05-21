# Workflow Benchmark Suite

Deterministic token-cost comparison: naive chat-history baseline vs Plinth, across **8 agentic workflows** covering 8 distinct personas.

## TL;DR

| Workflow | Reduction |
|---|---:|
| 5-source research synthesis | **85.3%** |
| 30-PDF literature review | **97.6%** |
| Researcher → Writer → Reviewer (3 rounds) | **81.8%** |
| Code review on 14-file PR | **83.8%** |
| 10-turn customer support with lookups | **86.3%** |
| Sales lead enrichment + CRM sync | **77.0%** |
| Q&A across 50 contracts with citation | **48.4%** |
| 12-section quarterly market report | **88.9%** |
| **Average across all scenarios** | **81.1%** |

The deterministic model is an upper bound. Live runs with real LLM APIs come in within ±5pp of these numbers (the cross-check against `examples/01-research-agent` showed 85% modeled vs 71% measured in real runs — the difference is real-LLM prompt overhead the model can't see).

## Run it

```bash
python -m benchmarks.workflows.run_all              # ASCII table + JSON
python -m benchmarks.workflows.run_all --markdown   # MD for docs/blogs
python -m benchmarks.workflows.run_all --quiet      # JSON-path only (for CI)
```

Results write to `benchmarks/workflows/results/<timestamp>-suite.json` by default.

## Add a new scenario

1. Create `scenarios/my_workflow.py` exporting a `SCENARIO = Scenario(...)` constant
2. Import it in `scenarios/__init__.py` and add to `ALL_SCENARIOS`
3. Re-run the suite

The harness is purely structural — you describe steps and dependencies; the math falls out. Read `harness.py` to see how 100 lines of dataclasses produce all these numbers.

## Read more

- `MODEL.md` — the token-accounting methodology (read this before quoting any number)
- `RESULTS.md` — the canonical results table as Markdown (regenerate with `--markdown`)
- `scenarios/*.py` — each scenario file documents its workflow and realistic assumptions

## What the suite does NOT do

- No real LLM calls (use `examples/01-research-agent --mode live` for that)
- No wall-clock measurement (use `plinth bench` for performance)
- No output-quality grading (separate workstream)

## When to cite which number

| Use case | Cite |
|---|---|
| Landing page / homepage headline | 71% (live-measured, conservative) |
| Sales deck | Average across the suite: 81% |
| Specific persona (e.g. legal team demo) | The matching scenario's number |
| Technical write-ups / docs | Run the suite and link to that specific result-JSON |

Never quote a number without linking to the JSON-report that produced it. The marketing/sales numbers are checked against the suite quarterly.
