# Benchmark Results

Each JSON file in this directory is a captured benchmark run. The newest `baseline-*.json` is the canonical baseline; older ones are kept for trend analysis.

The current canonical baseline is **`baseline-v1.1.json`** (services v1.1.0, run captured 2026-05-10).

## File layout

```
results/
├── baseline-v1.1.json       # canonical: README "Performance" table is generated from this
├── raw-v1.1/                # per-workload JSONs from the run (one per workload + a suite JSON)
└── README.md                # this file
```

`baseline-v1.1.json` is the human-shaped roll-up: one `results[]` entry per workload with `p50_ms / p95_ms / p99_ms / error_rate / rps_target / rps_achieved`, plus `host`, `git_sha`, `services_version`, `profile`, and `environment` metadata. The matching per-workload JSONs in `raw-v1.1/` carry the full per-second `buckets[]` series — useful for plotting and digging into where latency built up during the ramp.

## Reproducing a baseline

```bash
make stop && rm -rf /tmp/plinth-data
make serve
sleep 5  # wait for services to settle
make bench-quick OUTPUT=benchmarks/results/baseline-v1.1.json
make stop
```

Notes:

- Set `PLINTH_RATE_LIMITS_ENABLED=false` on the gateway when targeting RPS above the default per-agent cap (60 RPM), or pass an `--auth-token` minted with `agent_id=null` to bypass the per-agent bucket.
- The bench harness needs an auth token for the gateway workloads. Mint one via `POST /v1/tokens` against the identity service, then pass it through `--auth-token`.

## Comparing two runs

```bash
make bench-compare BASELINE=benchmarks/results/baseline-v1.0.json LATEST=benchmarks/results/baseline-v1.1.json
```

(Internally this is `plinth-bench compare A.json B.json` and renders a markdown table of deltas.)

## Hardware notes

Baselines are captured on a developer machine (single MacBook), not a production cluster. They show **comparative trends across versions on the same hardware**, not absolute production-grade numbers. For real production targets, see [`docs/slos.md`](../../docs/slos.md).

The `host` field inside each baseline JSON records the actual machine the run was captured on — when comparing two baselines, sanity-check that `host.cpu` and `host.cores` match before reading deltas as performance regressions.
