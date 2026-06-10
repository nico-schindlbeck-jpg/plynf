# Simulated-workload verification replay — 2026-06-10

**What this is.** A frontier-model replay of the quality-verification
harness over three realistic, deliberately bloated workloads (orders API,
Salesforce, Slack incident thread) — 19 agent questions, each answered
twice: once from the **raw** tool response, once from the **shaped** one.
The answering model saw only one context at a time; answers were recorded
verbatim in `answers.run*.json` and scored by the canonical harness rules
(`plinth_proxy.verify`).

**What this is NOT.** Real customer traffic. Payloads are realistic
replicas of the respective API schemas, not production data. This replay
de-risks the mechanism; it does not replace the real-workload validation
(P0.3) with a design partner.

## Reproduce

```bash
# Emit the contexts (answer them with any model you like):
python scripts/verify_replay.py contexts services/proxy/examples/verify/salesforce.spec.yaml

# Score recorded answers with the canonical rules:
python scripts/verify_replay.py report \
    services/proxy/examples/verify/salesforce.spec.yaml \
    services/proxy/examples/verify/replays/2026-06-10-simulated/answers.run2.json
```

Or run fully automated against any OpenAI-compatible endpoint:

```bash
python -m plinth_proxy.verify services/proxy/examples/verify/salesforce.spec.yaml \
    --base-url http://localhost:11434/v1 --model llama3.1
```

## The two runs

* **Run 1** — policies exactly as shipped in v1.8.0. The harness caught:
  1. **A real answer regression:** "Which competitor are we up against?"
     answers *Samsara* from raw context, *UNKNOWN* from shaped —
     `Competitor__c` was missing from the `get_opportunity` keep-list.
  2. **A schema mismatch:** the Slack keep-list used bare field names
     (`user`, `text`, `ts`) that don't traverse the Web-API envelope
     (`messages[]`). The safe fallback prevented any answer change
     (answers stayed equivalent), but savings degraded to lossless-only —
     exactly the failure mode the fallback exists for.
* **Run 2** — after the two policy fixes
  (`Competitor__c|competitor|competitors` added; Slack paths changed to
  `messages.*` with bare-name synonyms): **all 19/19 answers equivalent.**

The point of publishing both runs: the harness is not a rubber stamp —
it found a real gap in our own shipped defaults on first contact, and the
fix workflow (flag → extend keep-list → re-run → green) is the product's
intended operating loop.
