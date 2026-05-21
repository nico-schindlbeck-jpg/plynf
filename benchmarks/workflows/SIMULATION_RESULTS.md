# Workflow Benchmark — Monte-Carlo Results

`100 runs/scenario × 8 scenarios = 800 total runs · seed=42 · generated 2026-05-21T15:26:05`

## Headline number

**Average token reduction: 80.3%** across 800 simulated runs.

- Median: 83.3%
- 95% confidence range: 45.7% — 97.5%
- IQR (typical case): 78.6% — 87.4%
- Standard deviation: 14.1 percentage points

## Per scenario

| Scenario | Persona | Mean | Median | p25 | p75 | p95 | σ | $ saved (avg) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 5-source research synthesis | Hobby developer / research bot | **84.4%** | 84.8% | 83.6% | 85.5% | 86.1% | 1.5pp | $0.87 |
| 30-PDF literature review | Analyst / research-heavy knowledge worker | **97.4%** | 97.4% | 97.2% | 97.6% | 97.6% | 0.3pp | $72.44 |
| Researcher → Writer → Reviewer (3 rounds) | Content automation / 3-agent pipeline | **81.9%** | 81.9% | 81.3% | 82.5% | 83.2% | 0.9pp | $0.80 |
| Code review on 14-file PR | Engineering / code-review bot | **82.5%** | 83.4% | 81.3% | 85.1% | 87.2% | 4.0pp | $0.81 |
| 10-turn customer support with 4 lookups | Customer support / tier-1 automation | **83.5%** | 84.6% | 81.9% | 85.8% | 86.9% | 2.9pp | $0.56 |
| Sales lead enrichment + CRM sync | Sales ops / lead research | **77.3%** | 77.4% | 76.1% | 78.6% | 79.8% | 1.6pp | $0.39 |
| Q&A across 50 contracts with citation | Legal ops / internal knowledge bot | **47.0%** | 47.0% | 43.4% | 51.1% | 55.8% | 5.7pp | $0.08 |
| 12-section quarterly market report | Strategy / market research auto-writer | **88.7%** | 88.8% | 88.5% | 89.2% | 89.7% | 0.7pp | $3.45 |
| **All scenarios pooled** | — | **80.3%** | 83.3% | 78.6% | 87.4% | 97.5% | 14.1pp | $9.93 |

## What we cite where

| Surface | Claim | Source |
|---|---|---|
| Hero (one number) | `~80% average` | this run, mean |
| Pricing CTA / sales deck | `78-87% typical, 80% average` | this run, IQR + mean |
| Honest range (worst case) | `min run: 31.6%` | this run, min |

## Methodology

Each scenario template (see `scenarios/`) is perturbed with two kinds of variance:

1. **Token-size variance**: every step's `produces_tokens` is multiplied by a log-normal factor centred on 1.0 with σ tuned per scenario (0.22 to 0.40 — see `simulation.py:VARIANCE_RECIPES`).
2. **Step-count variance**: workflows with naturally variable counts (number of sources fetched, files in a PR, turns in a conversation) sample from realistic ranges. A 5-source research task might run on 3 or 8 sources; a code review might cover 5 or 25 files.

The seed is fixed (`42`) — re-running this script reproduces the exact numbers above. To regenerate a fresh set, change seed and document the reason in your commit message.

