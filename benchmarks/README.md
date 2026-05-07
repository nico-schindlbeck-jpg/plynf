# plinth-bench

Stress benchmarks for the Plinth services (workspace + gateway + identity).

## Why

Numbers in CONTRACTS.md are claims; numbers in `benchmarks/results/` are measurements. This package is the harness that produces measurements.

It also pairs with the `LoadShedder` middleware in `services/workspace` and `services/gateway` — the two together mean the platform answers two questions: *how much can it take?* and *what does it do when it gets too much?*

## Install

```bash
$VENV/bin/pip install -e "./benchmarks[dev]"
```

## Usage

```bash
# Single workload
plinth-bench workspace_kv \
  --base-url http://localhost:7421 \
  --target-rps 500 \
  --ramp-seconds 30 \
  --hold-seconds 60 \
  --output results/workspace_kv_$(date +%s).json

# Quick smoke (~25 s per workload)
plinth-bench workspace_kv --target-rps 50 --hold-seconds 5 --ramp-seconds 5 --cooldown-seconds 0

# Full standard suite (writes per-workload JSONs + a combined suite JSON)
plinth-bench all --output-dir benchmarks/results

# Compare two runs (single-run JSON or suite JSON)
plinth-bench compare results/A.json results/B.json

# List available workloads
plinth-bench list
```

The harness is open-loop: a slow server makes RPS-observed drop, not request rate climb. This is the right behaviour for real overload scenarios where load doesn't politely back off.

## Workloads

| name                        | service     | what it stresses                       |
|-----------------------------|-------------|----------------------------------------|
| `workspace_kv`              | workspace   | KV PUT/GET (versioned writes)          |
| `workspace_files`           | workspace   | 4 KB blob PUT + meta GET               |
| `workspace_snapshot`        | workspace   | snapshot creation (heaviest workload)  |
| `gateway_invoke_cached`     | gateway     | invoke hot path (cache hit dominated)  |
| `gateway_invoke_cold`       | gateway     | invoke proxy path (cache miss)         |
| `identity_token_issue`      | identity    | JWT signing throughput                 |

## Output format

```json
{
  "workload": "workspace_kv",
  "target_url": "http://localhost:7421",
  "target_rps": 500,
  "ramp_seconds": 30,
  "hold_seconds": 60,
  "cooldown_seconds": 10,
  "started_at": "2026-05-07T...",
  "finished_at": "2026-05-07T...",
  "total_requests": 30000,
  "successful": 29850,
  "failed": 150,
  "error_rate": 0.005,
  "latency_ms": {"p50": 4.2, "p95": 18.7, "p99": 42.1, "max": 120.4, "mean": 6.5},
  "buckets": [{"t": 0, "rps_observed": 12, "p50_ms": 3.8, ...}, ...],
  "errors_by_type": {"503": 100, "timeout": 50}
}
```

## Testing the harness

```bash
$VENV/bin/pytest benchmarks
```

Tests do not require running services — the runner is exercised against an in-process FastAPI app via `ASGITransport`.
