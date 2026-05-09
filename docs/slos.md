# Plinth Service Level Objectives — v1.0

> **Audience**: SREs and operators running Plinth in production. Product
> engineering also reads this to understand what guarantees the platform
> claims and how those guarantees are measured.

This document defines the published SLOs for Plinth v1.0, how they are
measured, the error-budget calculation, and the escalation policy. It is
intentionally short and operator-actionable: every line below should be
something you can write a Grafana alert against.

## Methodology

Every SLO is defined as a 4-tuple:

* **Target**: the threshold (latency, availability percentage, ratio, etc.).
* **Measurement**: the exact PromQL or audit-log query, so two engineers
  measuring at different times always agree on whether the SLO held.
* **Window**: the rolling time window over which the target is evaluated.
  We use 1h or 24h for latency/throughput and 30 days for availability —
  matching Prometheus retention defaults.
* **Reporting**: where the result is surfaced (Prometheus, dashboard,
  audit query, monthly report).

A request counts as **good** if it succeeded AND completed under the
threshold; **bad** otherwise. SLO availability = `good / total` over the
window. The error budget is `1 - target` of the window.

## Service Level Objectives

The targets below are **not aspirational**: they reflect what the v1.0
codebase has been load-tested against in the reference SQLite + 4 vCPU
deployment used for our CI benchmarks. Operators running on Postgres with
real network latency should retune the targets to their environment after
a one-week burn-in.

### Workspace service

| #  | SLO                                    | Target              | Measurement                                                  | Window  | Reporting                                  |
|---:|----------------------------------------|---------------------|--------------------------------------------------------------|---------|--------------------------------------------|
| 1  | KV read latency                        | p99 < 50 ms         | `histogram_quantile(0.99, sum by (le) (rate(plinth_http_request_duration_seconds_bucket{service="workspace",method="GET",path=~".*/kv/.*"}[1h])))` | 1h      | Prometheus + dashboard tile                |
| 2  | KV write latency                       | p99 < 80 ms         | Same as #1 with `method="PUT"`                                | 1h      | Prometheus + dashboard                     |
| 3  | KV delete latency                      | p99 < 80 ms         | Same as #1 with `method="DELETE"`                             | 1h      | Prometheus                                 |
| 4  | File read latency                      | p99 < 200 ms        | `histogram_quantile` over `path=~".*/files/.*"` GET           | 1h      | Prometheus                                 |
| 5  | File write latency                     | p99 < 500 ms        | Same as #4 with `method="PUT"`                                | 1h      | Prometheus                                 |
| 6  | Workspace list latency                 | p99 < 100 ms        | `path="/v1/workspaces"` GET                                   | 1h      | Prometheus                                 |
| 7  | Workspace creation availability        | 99.9%               | `(1 − rate(5xx)/rate(*)) on POST /v1/workspaces`              | 30 days | Prometheus + monthly review                |
| 8  | Workspace KV availability              | 99.95% (3.5 nines)  | `(1 − 5xx-rate − connection-failure-rate) on /v1/workspaces/*/kv` | 30 days | Prometheus                                 |
| 9  | Workflow step lease acquisition        | p95 < 100 ms        | Histogram of `POST /v1/workspaces/{id}/leases/acquire`        | 1h      | Prometheus                                 |
| 10 | Workflow step lease availability       | 99.9%               | (1 − 5xx-rate) on `/v1/workspaces/{id}/leases/acquire`        | 30 days | Prometheus                                 |
| 11 | Workspace load-shed rate               | < 0.5% of requests  | `rate(plinth_load_shed_total[5m]) / rate(plinth_http_requests_total[5m])` | 5m | Prometheus + on-call alert     |

### Gateway service

| #  | SLO                                    | Target              | Measurement                                                  | Window  | Reporting                                  |
|---:|----------------------------------------|---------------------|--------------------------------------------------------------|---------|--------------------------------------------|
| 12 | Tool invoke (cache hit)                | p99 < 30 ms         | `histogram_quantile(0.99, …{cached="true"})` on `plinth_tool_invocation_duration_seconds_bucket` | 1h | Prometheus + dashboard       |
| 13 | Tool invoke (cache miss, gateway-side) | p99 < 200 ms        | Same as #12 with `cached="false"`, with upstream-RTT subtracted by `plinth_tool_upstream_duration_seconds` if exported | 1h | Prometheus     |
| 14 | Tool invoke availability               | 99.9%               | (1 − 5xx-rate) on `POST /v1/invoke`                           | 30 days | Prometheus                                 |
| 15 | Cache hit rate (read-heavy workloads)  | > 60%               | `sum(rate(plinth_tool_invocations_total{cached="true"}[1h])) / sum(rate(plinth_tool_invocations_total[1h]))` | 1h | Prometheus + dashboard tile  |
| 16 | Audit-chain integrity                  | 100% verified daily | `plinth_audit_chain_verified == 1` after the 03:00 UTC verify cron | 1d | Prometheus alert              |
| 17 | OTLP emission success                  | > 99%               | `1 − (rate(plinth_otlp_flush_errors_total[1h]) / rate(plinth_otlp_events_emitted_total[1h]))` | 1h | Prometheus                       |
| 18 | Rate-limit rejection rate              | < 1% of requests    | `rate(plinth_rate_limit_rejections_total[5m]) / rate(plinth_http_requests_total[5m])` | 5m | Prometheus + ticket          |

### Identity service

| #  | SLO                                    | Target              | Measurement                                                  | Window  | Reporting                                  |
|---:|----------------------------------------|---------------------|--------------------------------------------------------------|---------|--------------------------------------------|
| 19 | Token issuance latency                 | p99 < 30 ms         | `path="/v1/tokens"` POST                                      | 1h      | Prometheus                                 |
| 20 | Token verification latency             | p99 < 20 ms         | `path="/v1/tokens/verify"` POST                               | 1h      | Prometheus                                 |
| 21 | JWKS retrieval latency                 | p99 < 50 ms         | `path="/v1/.well-known/jwks.json"` GET                        | 1h      | Prometheus                                 |
| 22 | Token verification availability        | 99.95%              | (1 − 5xx-rate) on `/v1/tokens/verify`                          | 30 days | Prometheus + customer SLA derivation       |
| 23 | Key rotation freshness                 | < 24h               | `time() - plinth_key_last_rotation_unix_seconds`               | 1h      | Prometheus + ops report                    |

### Cross-service / dashboard

| #  | SLO                                    | Target              | Measurement                                                  | Window  | Reporting                                  |
|---:|----------------------------------------|---------------------|--------------------------------------------------------------|---------|--------------------------------------------|
| 24 | Service availability (workspace, gateway, identity) | 99.9% (3 nines) | `up{job="plinth"}` over the window                  | 30 days | Prometheus + monthly review                |
| 25 | Dashboard overview API latency         | p99 < 800 ms        | `path="/api/overview"` GET                                    | 1h      | Prometheus                                 |
| 26 | Dashboard upstream poll success        | > 99%               | `1 - (rate(plinth_dashboard_upstream_failures_total[1h]) / rate(plinth_dashboard_polls_total[1h]))` | 1h | Prometheus                       |

That is **26 published SLOs** across 4 services. The most operator-relevant
ones are #1, #2, #12, #14, #16, #20, and #24 — these are the SLOs you
should alert on first.

## Error budget

Each SLO has an associated **error budget**: the fraction of the
measurement window in which the objective may be violated without burning
through the SLO.

| Target         | 30-day budget | 1-day budget |
|----------------|--------------:|-------------:|
| 99.9%          | 43.2 minutes  | 1.4 minutes  |
| 99.95%         | 21.6 minutes  | 43.2 seconds |
| 99.99%         | 4.32 minutes  | 8.64 seconds |

For latency targets (`p99 < N ms`) the budget is the 1% of requests
allowed to exceed the threshold per window — alert on a sustained breach,
not on a single sample.

We follow the [Google SRE error-budget burn-rate](https://sre.google/workbook/alerting-on-slos/)
convention with two configured rates:

* **Fast burn** (page): 14.4× consumption rate over a 1h window. If
  sustained you'll burn the entire monthly budget in <2 days.
* **Slow burn** (ticket): 6× consumption rate over a 6h window. A
  degradation that won't immediately cause customer pain but trends bad
  if not addressed within the day.

A sample alerting rule (Prometheus YAML lives in
`deploy/prometheus/alerts/slo.yaml`):

```yaml
- alert: WorkspaceKVReadP99FastBurn
  expr: |
    histogram_quantile(0.99,
      sum by (le) (rate(plinth_http_request_duration_seconds_bucket{
        service="workspace", method="GET",
        path=~".*/kv/.*"
      }[5m]))) > 0.05
  for: 10m
  labels:
    severity: page
    slo: workspace_kv_read_latency
  annotations:
    summary: "Workspace KV read p99 > 50ms for 10m"
    runbook: docs/runbooks/workspace_kv_slow.md

- alert: GatewayCacheHitRateLow
  expr: |
    (
      sum(rate(plinth_tool_invocations_total{cached="true"}[1h])) /
      sum(rate(plinth_tool_invocations_total[1h]))
    ) < 0.6
  for: 1h
  labels:
    severity: ticket
    slo: gateway_cache_hit_rate
  annotations:
    summary: "Gateway cache hit rate < 60% over the last hour"

- alert: AuditChainBroken
  expr: plinth_audit_chain_verified == 0
  for: 5m
  labels:
    severity: page
    slo: audit_chain_integrity
  annotations:
    summary: "Audit hash chain failed verification"
    runbook: docs/runbooks/audit_chain_broken.md
```

## Escalation

* **Page** alerts go to the on-call SRE rotation via PagerDuty (default
  group `plinth-sre`). The runbook lives at `docs/runbooks/<slo-name>.md`.
* **Ticket** alerts open a JIRA ticket in the `PLINTH-OPS` project. They
  do *not* page; an SRE picks them up within one business day.
* For any SLO violation the on-call writes a brief incident report into
  `docs/incidents/YYYY-MM-DD-<slug>.md` within 72 hours.

## Alerting recommendations: when to page vs. ticket

| Type                                  | Action  | Rationale                                                    |
|---------------------------------------|---------|--------------------------------------------------------------|
| Service down (`up == 0`)              | Page    | Total customer impact. Always page.                          |
| Audit chain broken                    | Page    | Either tampering or bug in verifier — needs human triage now. |
| 5xx rate > 5% for 5 min               | Page    | Real customer pain.                                          |
| SLO fast-burn (any of #1–#26)         | Page    | Will exhaust monthly budget in days.                          |
| SLO slow-burn (any of #1–#26)         | Ticket  | Trend, not a fire.                                           |
| Cache hit rate < 60%                  | Ticket  | Performance degradation, not customer-blocking.              |
| Quota rejections climbing             | Ticket  | Tenant policy decision, not platform bug.                    |
| OTLP collector unreachable            | Ticket  | Platform keeps working, observability is degraded.           |
| Rate-limit rejection rate > 1%        | Ticket  | Likely a noisy tenant or undersized quota.                   |
| Disk usage > 80%                      | Ticket  | Capacity planning, not immediate pain.                       |
| Disk usage > 95%                      | Page    | Imminent failure.                                            |

## Runbook links

Every page-grade SLO has a runbook:

* **Workspace KV slow** — `docs/runbooks/workspace_kv_slow.md`
* **Workspace overload (load shedder firing)** — `docs/runbooks/workspace_overload.md`
* **Gateway invoke slow** — `docs/runbooks/gateway_invoke_slow.md`
* **Gateway cache hit rate low** — `docs/runbooks/gateway_cache_low.md`
* **Identity verify slow** — `docs/runbooks/identity_verify_slow.md`
* **Identity key rotation stuck** — `docs/runbooks/identity_key_rotation.md`
* **Audit chain broken** — `docs/runbooks/audit_chain_broken.md`
* **Service down** — `docs/runbooks/service_down.md`

Each runbook has the same skeleton: trigger, immediate impact, first
response (≤ 5 min), root-cause checklist (≤ 30 min), recovery, and
post-incident actions.

## Review cadence

* **Monthly**: SLO review meeting (SRE lead + product). Re-fit targets if
  a quarter consistently shows >50% budget consumption (target may be
  too aggressive) or <5% budget consumption (target may be too loose).
* **Quarterly**: customer SLA review. Customer-facing SLAs are derived from
  these SLOs but are intentionally looser (e.g. a 99.9% internal SLO →
  99.5% customer SLA) to give us margin for legitimate maintenance windows.
* **Annually**: full SLO catalogue review — retire SLOs whose underlying
  endpoint has been removed; add SLOs for new endpoints.

## What is NOT covered

* The `mock-mcp-server` sample tool latency is not part of any SLO. It's
  test infrastructure.
* The dashboard service is mostly an observability surface; its uptime is
  monitored as an SLI but not as a customer SLO target.
* MCP-server upstream latency (GitHub, Slack, Linear) is part-target-of-
  best-effort: we measure but do not commit to a target, because the
  upstream service is outside our control.
* Cross-region replication latency is target-of-best-effort in v1.0;
  multi-region SLOs land in v1.1.
* Cold-start latency on container restart — covered by deployment SLOs
  in `docs/deployment.md`, not here.

## Appendix: how to add a new SLO

When adding an SLO:

1. Pick an endpoint or operation that already emits metrics. If it
   doesn't, add the instrumentation first (one PR), then the SLO (next PR).
2. Pick a target by sampling 30 days of historical data and choosing
   the p95 or p99 — the SLO should be tight enough to alert on real
   regressions but loose enough that current production traffic doesn't
   trip it.
3. Write the PromQL query exactly as it will appear in the alert rule —
   not a paraphrase. Two operators reading the SLO must come up with the
   same alert.
4. Add the alert to `deploy/prometheus/alerts/slo.yaml` with both fast-
   and slow-burn variants.
5. Write a runbook stub in `docs/runbooks/<slo-name>.md` even if the
   first response is "wake up an SRE" — the runbook gets fleshed out as
   the SLO experiences its first incident.
