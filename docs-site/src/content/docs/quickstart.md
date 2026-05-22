---
title: Quickstart
description: Run Plynf locally and see the 71% token savings demo in five minutes.
section: guides
order: 1
sourceFile: README.md
---

Get Plynf running locally and watch the token-savings demo in about five minutes. No external services, no API keys required.

## Prerequisites

- Python 3.11+
- Node.js 20+
- `make`, `git`
- Optional: Docker if you want to run via `docker-compose`

## Install everything

```bash
git clone https://github.com/nico-schindlbeck-jpg/plinth.git
cd plinth

make install   # services + Python SDK + TypeScript SDK + examples
```

`make install` builds and installs:

- 4 platform services (Workspace, Gateway, Identity, Dashboard)
- 3 real MCP tool servers (GitHub, Slack, Linear)
- 1 mock MCP server (offline-capable, 6 demo tools)
- Python SDK (`plinth`)
- TypeScript SDK (`@plinth/sdk`)
- Example agents

## Run the test suite

```bash
make test
```

Expect ~2,867 tests passing across 7 SDKs (Python, TypeScript, Go, Swift, Kotlin) in well under a minute.

## Start the stack

```bash
make serve
```

Eight services start in the background:

| Component             | Port  |
|-----------------------|------:|
| Workspace service     |  7421 |
| Tool Gateway          |  7422 |
| Mock MCP server       |  7423 |
| Web Dashboard         |  7424 |
| Identity service      |  7425 |
| GitHub MCP server     |  7426 |
| Slack MCP server      |  7427 |
| Linear MCP server     |  7428 |

Open the dashboard:

```bash
open http://localhost:7424/
```

## Run the demos

### Demo 01 — token comparison (the headline)

```bash
make demo
```

You should see something like:

```
═══════════════════════════════════════════════════════════════════
  TOKEN-USAGE COMPARISON — research-agent on topic "renewable energy"
═══════════════════════════════════════════════════════════════════
  Baseline (no Plynf):        23,704 tokens   |   $0.0810
  With Plynf:                  6,795 tokens   |   $0.0345
  ─────────────────────────────────────────────
  Reduction:                     71.3 %        |   $0.0464 saved
═══════════════════════════════════════════════════════════════════
```

Token counts are exact (`cl100k_base` via tiktoken). Cost estimates use Anthropic Sonnet pricing.

### Demo 02 — multi-agent handoff

```bash
make demo-handoff
```

Researcher → Writer → Reviewer collaborate via Plynf channels in 8.7k total tokens.

### Demo 03 — resumable workflow

```bash
make demo-resume
```

A 6-step pipeline crashes mid-flight; resume saves **32%** of the work versus restart-from-scratch.

### Demo 04 — GitHub triage

```bash
make demo-triage
```

GitHub-issue triage agent. Runs in simulation mode by default — no GitHub account needed.

### Demo 05 — durable workflow

```bash
# Terminal 1
plinth-workflow-worker --handlers-module handlers --concurrency 2

# Terminal 2
python examples/05-durable-workflow/start_workflow.py --topic "renewable energy"
```

## Stop everything

```bash
make stop
```

## What next?

- **Read** [Architecture](/docs/architecture) to understand how the pieces fit together.
- **Read** [API Stability](/docs/api-stability) to understand the v1 contract.
- **Browse** the SDK examples in `examples/` (each one has its own README).
- **Read** [SLOs](/docs/slos) and the [Threat Model](/docs/threat-model) before deploying for real.
