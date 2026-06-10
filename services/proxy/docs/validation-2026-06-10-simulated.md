# Quality validation — simulated workload replay (2026-06-10)

**Question answered:** *does shaping ever change the model's answer — and
how much does it save on realistic, bloated tool responses?*

**Method.** Three realistic workloads (orders API, Salesforce
Opportunity + Account, Slack incident thread; payloads mirror the real
API schemas — `attributes` wrapper, audit columns, blocks/reactions
noise), 19 agent questions. A frontier model answered every question
twice — once from the **raw** tool response, once from the **shaped**
one — seeing only one context at a time. Scoring, token counts
(tiktoken `o200k_base`, compact JSON) and regression listing come from
the canonical harness (`plinth_proxy.verify`). Everything is committed
and reproducible: specs in `examples/verify/*.spec.yaml`, recorded
answers + per-run policy snapshots + machine-readable reports in
`examples/verify/replays/2026-06-10-simulated/`.

**Honesty box.** This is a *simulated* workload — realistic schemas, not
real customer traffic. It de-risks the mechanism and exercises the
harness end-to-end; it does not replace validation on a design partner's
live agent (P0.3 in the engineering brief). Sample size is 19 questions.

## Run 1 — policies exactly as shipped in v1.8.0

| Tool | raw → shaped tokens | saved |
|------|--------------------:|------:|
| get_order | 297 → 75 | −75 % |
| get_customer | 84 → 26 | −69 % |
| get_opportunity | 587 → 103 | −82 % |
| get_account | 506 → 72 | −86 % |
| get_channel_messages (Slack) | 654 → 591 | **−10 %** |
| **Total** | **2 128 → 867** | **−59.3 %** |

**Answers unchanged: 18/19 (94.7 %).** The harness surfaced two real
defects in our own shipped defaults:

1. **Answer regression (flagged):** *"Which competitor are we up against
   on this deal?"* — raw context answers **Samsara**, shaped context
   answers **UNKNOWN**. `Competitor__c` was missing from the
   `get_opportunity` keep-list, and competitor questions are routine for
   sales agents.
2. **Schema mismatch (visible in the token column):** the Slack keep-list
   used bare field names (`user`, `text`, `ts`) that don't traverse the
   Web-API envelope (`messages[]`). The engine's **safe fallback worked
   exactly as designed** — zero answer changes, the response was kept in
   its lossless-minimised form — but savings collapsed to −10 %.

## Fixes (both in `policies/`, both harness-driven)

- `salesforce.default.yaml` → `get_opportunity.allow_fields` +=
  `Competitor__c|competitor|competitors` (synonym-robust).
- `slack.default.yaml` → keep-list paths now traverse the envelope
  (`messages.user|user`, …; `search_messages` also covers
  `messages.matches.*`).

## Run 2 — after the fixes

| Tool | raw → shaped tokens | saved |
|------|--------------------:|------:|
| get_order | 297 → 75 | −75 % |
| get_customer | 84 → 26 | −69 % |
| get_opportunity | 587 → 112 | −81 % |
| get_account | 506 → 72 | −86 % |
| get_channel_messages (Slack) | 654 → 191 | **−71 %** |
| **Total** | **2 128 → 476** | **−77.6 %** |

**Answers unchanged: 19/19 (100 %). Regressions: none.**

## What this demonstrates

- **The two-layer guarantee holds under fire.** A broken keep-list
  (Slack) degraded savings, never correctness — the fallback refused to
  blank data.
- **The harness is not a rubber stamp.** On first contact it caught a
  genuine quality regression in shipped defaults, and the intended
  operating loop — *flag → extend keep-list → re-run → green* — closed it
  the same day.
- **The honest headline for tool-heavy agents:** on the shaped
  tool-response portion, expect **~70–86 % per tool with a keep-list**,
  lossless-only as the safe floor, with answer equivalence you can
  re-verify on your own data at any time:

```bash
python -m plinth_proxy.verify examples/verify/salesforce.spec.yaml \
    --base-url <any OpenAI-compatible endpoint> --model <model>
```
