# Plinth Observability — v1.0

> **Audience**: Operators wiring Plinth into their existing monitoring
> stack (Prometheus, Grafana, Datadog, Honeycomb), and developers debugging
> production incidents using the data the platform exposes.

Plinth has three first-class observability surfaces. Use all three; each
answers a different question:

1. **Prometheus metrics** — for SLO tracking, alerting, capacity planning.
   The "is the system healthy?" surface.
2. **OTLP logs** — for per-event structured logs of every tool invocation.
   The "what exactly did agent X do at 14:32:07?" surface.
3. **Audit chain** — for tamper-evident attribution. The "can we prove
   what happened?" surface, used for compliance + incident forensics.

The first two are designed to be plumbed into your existing observability
stack with zero Plinth-specific tooling. The third is queried via the
gateway's `/v1/audit/*` REST endpoints + the dashboard.

---

## Surface 1: Prometheus metrics

Every Plinth service exposes `GET /metrics` returning the standard
Prometheus text exposition format (v0.0.4). The endpoint is unauthenticated
by design — Prometheus scrapers don't authenticate, and the data is
non-sensitive aggregate counters/gauges.

### Common metrics (every service)

```
plinth_http_requests_total{service, method, status, path}             # counter
plinth_http_request_duration_seconds_bucket{service, method, le}      # histogram
plinth_build_info{service, version, python_version}                   # gauge (always 1)
```

### Per-service metrics

**Workspace** (`services/workspace`):

```
plinth_workspaces_total{tenant_id}
plinth_kv_writes_total{tenant_id}
plinth_files_writes_total{tenant_id}
plinth_storage_bytes{tenant_id}
plinth_workflows_active{tenant_id}
plinth_workflow_steps_total{state}
plinth_workers_active
plinth_load_shed_total{service}
```

**Gateway** (`services/gateway`):

```
plinth_tool_invocations_total{tool_id, tenant_id, cached, result}
plinth_tool_invocation_duration_seconds_bucket{tool_id, cached, le}
plinth_tool_invocation_cost_usd_total{tool_id, tenant_id}
plinth_oauth_connections_active{service}
plinth_rate_limit_rejections_total{tenant_id}
plinth_quota_rejections_total{tenant_id, quota}
plinth_audit_chain_verified{service}
plinth_load_shed_total{service}
```

**Identity** (`services/identity`):

```
plinth_tokens_issued_total{tenant_id}
plinth_tokens_revoked_total{service}
plinth_tokens_active{service}
plinth_token_verifications_total{result}
```

`result` is one of `ok | expired | revoked | invalid`.

**Dashboard** (`services/dashboard`):

```
plinth_dashboard_polls_total{endpoint}
plinth_dashboard_upstream_failures_total{upstream}
```

`upstream` is one of `workspace | gateway | identity | unknown`.

**MCP servers** (mock-mcp, github-mcp, slack-mcp, linear-mcp):

```
plinth_mcp_invocations_total{service, tool, result}
plinth_mcp_invocation_errors_total{service, tool}
plinth_mcp_invocation_duration_seconds_bucket{service, tool, le}
```

`result` is one of `ok | error`. The `service` label distinguishes the four
MCP servers; the `tool` label is the MCP tool ID (e.g. `web.fetch`,
`issues.create`).

### Cardinality warnings

Prometheus pricing scales with active series — every unique label-value
combination across a metric is a series. Plinth's labels are designed to
keep cardinality bounded:

* `tenant_id`, `workspace_id`, `agent_id`: typically tens to hundreds.
  Safe to use as labels for a single-tenant or small-multi-tenant
  deployment. Operators with thousands of tenants should aggregate to a
  per-tenant *summary* in the gateway and emit only that as a metric.
* `tool_id`: bounded by the gateway's tool registry (≤ 100 in practice).
* `path`: a concrete request path. The middleware does *not* template
  path parameters (`/v1/workspaces/{id}/kv/{key}`) — operators are
  expected to aggregate via PromQL `sum by (method)` to collapse
  parameter values. If your tenant churn is high enough that the raw
  `path` cardinality bites, drop the `path` label at the Prometheus
  scrape config: `metric_relabel_configs: [{regex: 'path', action: labeldrop}]`.
* `status`: 2xx/4xx/5xx ranges → ~10 distinct values, safe.

A healthy Plinth deployment carries ~5,000–20,000 active series across
all 4 services; an unhealthy one (high tenant churn + raw paths) can
balloon to 100k+. Watch `prometheus_tsdb_symbol_table_size_bytes` over
time as your canary.

### Suggested scrape intervals

| Service           | Recommended | Why                                                  |
|-------------------|------------:|------------------------------------------------------|
| Workspace         | 15s         | Many counters change quickly under load.             |
| Gateway           | 15s         | Same as workspace; this is the hot path.             |
| Identity          | 30s         | Token verifies are fast counters but lower volume.   |
| Dashboard         | 60s         | Mostly polling — not a hot path.                     |
| MCP servers       | 30s         | Latency-bucketed but not very chatty.                |

15s is the canonical Prometheus default. Going below 10s rarely improves
fidelity and increases scrape cost linearly.

### Wiring to Prometheus

A minimal `prometheus.yml` snippet to scrape every Plinth service:

```yaml
scrape_configs:
  - job_name: plinth
    metrics_path: /metrics
    scrape_interval: 15s
    static_configs:
      - targets:
          - workspace:7421
          - gateway:7422
          - identity:7423
          - dashboard:7424
        labels:
          environment: production
```

Sample Grafana dashboards live in `deploy/grafana/`. Import them via
`Grafana → Dashboards → Import → Upload JSON`. The repository ships:

* `plinth-overview.json` — service health, request rate, error rate,
  cost-per-tenant, audit-chain status.
* `plinth-slo.json` — every SLO from `docs/slos.md` rendered as a panel
  with the matching alert rule.
* `plinth-tenants.json` — per-tenant resource consumption.

---

## Surface 2: OTLP logs

When `PLINTH_OTLP_ENABLED=true` is set on the gateway it forwards every
audit event to an OpenTelemetry collector as an OTel `LogRecord`. This is
**purely additive**: the existing audit table is still written, and
emission is best-effort (collector down → counted, never crashes a tool
call).

### Canonical OTLP attribute set (v1.0)

This is the contract between Plinth and downstream consumers. Any field
documented here is guaranteed to be present in the indicated scope.

#### Common (every event)

| Attribute        | Type   | Example                | Notes                                        |
|------------------|--------|------------------------|----------------------------------------------|
| `service.name`   | string | `plinth-gateway`       | Always set. `service.name` resource attribute too. |
| `service.version`| string | `1.0.0`                | Tracks the gateway package `__version__`.    |
| `region.id`      | string | `us-west-2`            | Only set when `PLINTH_REGION_ID` is configured. |

#### Per-tenant scope

| Attribute    | Type   | Notes                                                    |
|--------------|--------|----------------------------------------------------------|
| `tenant.id`  | string | Always present when the request was authenticated.       |
| `agent.id`   | string | Identifies which agent under the tenant made the call.   |

#### Per-workflow scope

| Attribute        | Type   | Notes                                            |
|------------------|--------|--------------------------------------------------|
| `workflow.id`    | string | Set when the call is part of a workflow step.    |
| `workflow.step`  | string | Step name, matching the workflow manifest entry. |

#### Per-tool scope

| Attribute              | Type    | Notes                                                  |
|------------------------|---------|--------------------------------------------------------|
| `tool.id`              | string  | E.g. `weather.lookup`.                                 |
| `tool.cached`          | bool    | True if served from gateway cache.                     |
| `tool.duration_ms`     | int     | End-to-end (includes upstream RTT).                    |
| `tool.cost_usd`        | float   | Estimated cost from `pricing.py`.                      |

#### Per-workspace scope

| Attribute        | Type   | Notes                                                  |
|------------------|--------|--------------------------------------------------------|
| `workspace.id`   | string | Set when the tool runs against a specific workspace.   |

#### Other

| Attribute            | Type    | Notes                                                |
|----------------------|---------|------------------------------------------------------|
| `arguments.hash`     | string  | SHA-256 over the JSON-canonical arguments.           |
| `arguments.preview`  | string  | Truncated to 500 chars.                              |
| `result.hash`        | string  | SHA-256 of the JSON result.                          |
| `error.message`      | string  | Set on failed invocations only.                      |
| `audit.id`           | string  | The matching `audit_events.id` row.                  |

### Wiring to OTLP collectors

**Datadog** — point the gateway at the Datadog OTLP intake:

```bash
export PLINTH_OTLP_ENABLED=true
export PLINTH_OTLP_ENDPOINT="https://otlp.datadoghq.com"
export PLINTH_OTLP_HEADERS_JSON='{"DD-API-KEY":"<your-key>"}'
```

Datadog auto-indexes the `tool.id`, `tenant.id`, `workspace.id` attributes
and you can search with `@tool.id:weather.lookup`.

**Grafana Loki** — go via the Grafana Agent (which understands OTLP):

```yaml
# grafana-agent config
logs:
  configs:
    - name: plinth
      clients:
        - url: https://loki:3100/loki/api/v1/push
otelcol:
  receivers:
    otlp:
      protocols:
        http:
          endpoint: 0.0.0.0:4318
  exporters:
    loki:
      endpoint: http://loki:3100/loki/api/v1/push
```

**Honeycomb** — Honeycomb's OTLP/HTTP endpoint accepts the gateway's
events directly:

```bash
export PLINTH_OTLP_ENDPOINT="https://api.honeycomb.io"
export PLINTH_OTLP_HEADERS_JSON='{"x-honeycomb-team":"<your-key>"}'
```

### Sampling

Plinth does NOT sample by default — every audit event is emitted.
Operators with extreme volumes should tune at the OTLP collector layer
(Otel collector `tail_sampling_processor`) rather than dropping events
inside Plinth: the audit table is still the system of record and we
don't want sampling logic in two places.

---

## Surface 3: Audit chain

The gateway's `audit_events` table carries every tool invocation with a
SHA-256 hash chain (each row's `hash = SHA256(prev_hash || canonical_row)`)
so tampering is detectable. The chain is verified daily by
`POST /v1/audit/verify` (cron-safe), and a 0/1 result is exposed as
`plinth_audit_chain_verified`.

For incident forensics:

```bash
# List recent invocations for a tenant
curl 'http://gateway:7422/v1/audit?tenant_id=acme&limit=100'

# Verify the chain end-to-end (synchronous, returns first divergence)
curl -X POST 'http://gateway:7422/v1/audit/verify'

# Get aggregate stats
curl 'http://gateway:7422/v1/audit/stats?tenant_id=acme'
```

Audit retention is configured via `PLINTH_AUDIT_RETENTION_DAYS` (default
365). Older rows are pruned by the daily compliance sweep but the chain
hashes still verify across the gap because each kept row's `prev_hash`
is preserved.

---

## Alerting recommendations

Minimum alert set every production deployment should run:

* **Service down** — `up{job="plinth"} == 0` for 2 min → page.
* **High error rate** — 5xx-rate > 5% for 5 min → page.
* **SLO fast-burn** — see `docs/slos.md`.
* **Audit chain broken** — `plinth_audit_chain_verified == 0` → page.
  This indicates either a bug in the daily verifier or actual tampering.
* **Quota rejections climbing** — `rate(plinth_quota_rejections_total[1h]) > 0.1`
  → ticket. Means a tenant is bumping their cap and the operator should
  decide whether to raise it.
* **OTLP collector unreachable** — `rate(otlp_flush_errors[10m]) > 0` →
  ticket. Plinth keeps working but downstream observability is degraded.

A complete `prometheus-rules.yaml` ships in `deploy/prometheus/alerts/`.

### Alert recipe library

Common alerts every operator should consider, grouped by symptom:

```promql
# 1. Service down — page on any service unreachable for 2 min.
up{job="plinth"} == 0

# 2. High error rate — 5xx > 5% of requests for 5 min.
sum(rate(plinth_http_requests_total{status=~"5.."}[5m]))
  / sum(rate(plinth_http_requests_total[5m])) > 0.05

# 3. Latency p99 too high — Workspace KV reads exceeding 50ms p99.
histogram_quantile(0.99,
  sum by (le) (rate(plinth_http_request_duration_seconds_bucket{
    service="workspace", method="GET", path=~".*/kv/.*"
  }[5m]))) > 0.05

# 4. Load shedder firing — anything > 1/sec for 5m.
rate(plinth_load_shed_total[5m]) > 1

# 5. Cache hit rate fallen — cache-eligible workload getting < 60% hits.
sum(rate(plinth_tool_invocations_total{cached="true"}[1h]))
  / sum(rate(plinth_tool_invocations_total[1h])) < 0.6

# 6. No active workers — workflow processing has stopped.
sum(plinth_workers_active{status="active"}) == 0

# 7. Audit chain broken — daily verifier failed.
plinth_audit_chain_verified == 0

# 8. OTLP collector unreachable — observability degraded.
rate(plinth_otlp_flush_errors_total[10m]) > 0

# 9. Cost guardrail — per-tenant spend > $100/h.
sum by (tenant_id) (
  rate(plinth_tool_invocation_cost_usd_total[1h])
) > 100

# 10. Token verification slow — auth is a hot path.
histogram_quantile(0.99,
  sum by (le) (rate(plinth_http_request_duration_seconds_bucket{
    service="identity", path="/v1/tokens/verify"
  }[5m]))) > 0.02
```

### Sample Grafana dashboard layout

Paste the following description into the import-JSON wizard, or load the
prebuilt JSON from `deploy/grafana/plinth-overview.json`. The recommended
panel layout:

* **Row 1: Health** (4 stat panels)
  * `up{job="plinth"}` — 4 panels, one per service.
  * Color: red on 0, green on 1.
* **Row 2: Throughput** (1 graph)
  * `sum(rate(plinth_http_requests_total[5m])) by (service)` — stacked.
* **Row 3: Latency** (3 graphs)
  * Workspace KV p50/p95/p99
  * Gateway invoke p99 split by `cached`
  * Identity verify p99
* **Row 4: Cost & cache**
  * Cost per tenant per hour (heatmap by tenant_id).
  * Cache hit rate gauge.
* **Row 5: Errors & shedding**
  * 5xx rate per service
  * Load-shed rate per service
  * Quota rejections per tenant

### OTLP receiver setup — quick recipes

For each downstream backend, the env vars + collector config that work
out of the box:

**Datadog**:
```bash
export PLINTH_OTLP_ENABLED=true
export PLINTH_OTLP_ENDPOINT="https://otlp.datadoghq.com"
export PLINTH_OTLP_HEADERS_JSON='{"DD-API-KEY":"<your-key>"}'
```

**Honeycomb**:
```bash
export PLINTH_OTLP_ENABLED=true
export PLINTH_OTLP_ENDPOINT="https://api.honeycomb.io"
export PLINTH_OTLP_HEADERS_JSON='{"x-honeycomb-team":"<your-key>"}'
```

**Tempo / Grafana Cloud**:
```bash
export PLINTH_OTLP_ENDPOINT="https://otlp-gateway-prod-us-central-0.grafana.net/otlp"
export PLINTH_OTLP_HEADERS_JSON='{"Authorization":"Basic <base64>"}'
```

**Self-hosted otel-collector** (most common):
```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
exporters:
  loki:
    endpoint: http://loki:3100/loki/api/v1/push
service:
  pipelines:
    logs:
      receivers: [otlp]
      exporters: [loki]
```

Then point Plinth at it: `PLINTH_OTLP_ENDPOINT=http://otel-collector:4318`.

### Retention windows

Plinth's three observability surfaces have **different retention regimes**.
Operators should plan storage with these in mind:

| Surface          | Default retention            | Storage location         | How to extend                                                         |
|------------------|------------------------------|--------------------------|-----------------------------------------------------------------------|
| Prometheus       | 15 days (Prometheus default) | TSDB on the Prom server  | `--storage.tsdb.retention.time=90d`                                   |
| OTLP logs        | Decided by your collector    | External (DD, Honeycomb) | Backend-specific (Datadog defaults to 15 days; Honeycomb to 60 days) |
| Audit chain      | 365 days                     | Gateway DB (SQLite/PG)   | `PLINTH_AUDIT_RETENTION_DAYS=730`                                     |
| Dashboard cache  | 60 seconds                   | In-process               | not configurable; the dashboard is a read-through view                |

Each surface answers a different question, and they're meant to *overlap*:

* **Day 0–15**: all three surfaces have the data. Prefer Prometheus for
  aggregate views, OTLP for per-event, audit for compliance.
* **Day 15–60 (typical)**: Prometheus has expired; OTLP + audit remain.
* **Day 60–365**: only audit. The audit table is cheap to query (indexed
  on tenant + timestamp) so this is fine for compliance work.

Sizing guidance for the audit table: ~500 bytes per event × 365 days ×
expected throughput. For 100 invocations/sec sustained that's ~1.4 TB/year
in SQLite. Operators expecting that load should run on Postgres.

### Dashboard-as-instrument

The dashboard SPA at `:7424/` is *also* an observability surface for
operators without their own Prometheus stack. Useful built-in views:

* **Overview tile row** — service health + per-service request rate.
* **Trends row** — 4 mini-graphs for the last 24h: cost, p99 latency,
  error rate, cache hit rate. Click a tile to expand to a 7-day view.
* **Tenant rollup** — top-spending tenants over the last 24h.
* **Audit explorer** — paginated audit-log search by tenant/agent/tool.

For production deployments we still recommend a real Prometheus stack —
the dashboard is a read-through view of the gateway's audit log, so
its time-series are limited to the audit retention window and to whatever
the gateway buffers in its `/v1/audit?limit=10000` response.

---

## What changed in v1.0 (vs. v0.6)

* `/metrics` endpoint is **new** on every service.
* OTLP attributes were renamed for consistency: any code reading the
  legacy snake_case keys (`tool_id`, `workspace_id`, etc.) should update
  to the dotted form (`tool.id`, `workspace.id`). The legacy keys are
  also accepted on the input side so emitters with custom code keep working.
* `service.version` and `region.id` are new common attributes.
* `workflow.id` + `workflow.step` are new per-workflow attributes.
* Dashboard time-series tiles (`/api/timeseries`) are new.
