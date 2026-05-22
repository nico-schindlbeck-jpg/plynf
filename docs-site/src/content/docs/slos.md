---
title: SLOs
description: Plynf v1.0 service level objectives — 26 published targets across 4 services.
section: operations
order: 1
sourceFile: docs/slos.md
---

> **Audience**: SREs and operators running Plynf in production. Product engineering also reads this to understand what guarantees the platform claims and how those guarantees are measured.

This page summarises the published SLOs for Plynf v1.0, how they're measured, the error-budget calculation, and the escalation policy. It is intentionally short and operator-actionable: every target below is something you can write a Grafana alert against.

## Methodology

Every SLO is defined as a 4-tuple:

- **Target** — the threshold (latency, availability percentage, ratio, etc.).
- **Measurement** — the exact PromQL or audit-log query, so two engineers measuring at different times always agree on whether the SLO held.
- **Window** — the rolling time window over which the target is evaluated. We use 1h or 24h for latency/throughput and 30 days for availability.
- **Reporting** — where the result is surfaced (Prometheus, dashboard, audit query, monthly report).

A request counts as **good** if it succeeded AND completed under the threshold; **bad** otherwise. SLO availability = `good / total` over the window. The error budget is `1 - target` of the window.

## Workspace service

| #  | SLO                                    | Target              | Window  |
|---:|----------------------------------------|---------------------|---------|
| 1  | KV read latency                        | p99 < 50 ms         | 1h      |
| 2  | KV write latency                       | p99 < 80 ms         | 1h      |
| 3  | KV delete latency                      | p99 < 80 ms         | 1h      |
| 4  | File read latency                      | p99 < 200 ms        | 1h      |
| 5  | File write latency                     | p99 < 500 ms        | 1h      |
| 6  | Workspace list latency                 | p99 < 100 ms        | 1h      |
| 7  | Workspace creation availability        | 99.9%               | 30 days |
| 8  | Workspace KV availability              | 99.95% (3.5 nines)  | 30 days |
| 9  | Workflow step lease acquisition        | p95 < 100 ms        | 1h      |
| 10 | Workflow step lease availability       | 99.9%               | 30 days |
| 11 | Workspace load-shed rate               | < 0.5% of requests  | 5m      |

## Gateway service

| #  | SLO                                    | Target              | Window  |
|---:|----------------------------------------|---------------------|---------|
| 12 | Tool invoke (cache hit)                | p99 < 30 ms         | 1h      |
| 13 | Tool invoke (cache miss, gateway-side) | p99 < 200 ms        | 1h      |
| 14 | Tool invoke availability               | 99.9%               | 30 days |
| 15 | Cache hit rate (read-heavy workloads)  | > 60%               | 1h      |
| 16 | Audit-chain integrity                  | 100% verified daily | 1d      |
| 17 | OTLP emission success                  | > 99%               | 1h      |
| 18 | Rate-limit rejection rate              | < 1% of requests    | 5m      |

## Identity service

| #  | SLO                                    | Target              | Window  |
|---:|----------------------------------------|---------------------|---------|
| 19 | Token issuance latency                 | p99 < 30 ms         | 1h      |
| 20 | Token verification latency             | p99 < 20 ms         | 1h      |
| 21 | JWKS retrieval latency                 | p99 < 50 ms         | 1h      |
| 22 | Token verification availability        | 99.95%              | 30 days |
| 23 | Key rotation freshness                 | < 24h               | 1h      |

## Cross-service / dashboard

| #  | SLO                                    | Target              | Window  |
|---:|----------------------------------------|---------------------|---------|
| 24 | Service availability                   | 99.9% (3 nines)     | 30 days |
| 25 | Dashboard overview API latency         | p99 < 800 ms        | 1h      |
| 26 | Dashboard upstream poll success        | > 99%               | 1h      |

That is **26 published SLOs** across 4 services. The most operator-relevant ones are #1, #2, #12, #14, #16, #20, and #24 — these are the SLOs you should alert on first.

## Error budget

| Target         | 30-day budget | 1-day budget |
|----------------|--------------:|-------------:|
| 99.9%          | 43.2 minutes  | 1.4 minutes  |
| 99.95%         | 21.6 minutes  | 43.2 seconds |
| 99.99%         | 4.32 minutes  | 8.64 seconds |

For latency targets (`p99 < N ms`) the budget is the 1% of requests allowed to exceed the threshold per window — alert on a sustained breach.

## Reproducing benchmarks

Single-machine, single-uvicorn-worker localhost saturation captured in `benchmarks/results/baseline-v1.1.json`. Workspace KV/files and identity hot paths comfortably handle 500 RPS in single-digit ms. Run `make bench` (full sweep) or `make bench-quick` (100 RPS / 10 seconds) yourself.

For the full PromQL queries that back each SLO, see `docs/slos.md` in the repo.
