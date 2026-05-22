# Plynf Production-Readiness Checklist — v1.0

> **Audience**: enterprise SRE going live with Plynf. Treat this as a
> tick-box exercise: every item should be either ✅ or have a written
> remediation plan with owner + ETA.

This is the canonical pre-launch list. Make it part of the change-control
review for any environment promoted to "production". A representative
list of evidence is shown alongside each item — Grafana panel, vault
path, JIRA ticket, runbook URL, screenshot, etc.

## How to use

1. Fork this checklist into your launch ticket.
2. Walk it top-to-bottom; an unchecked box requires a written exception
   that's been signed off by the SRE lead.
3. Re-walk quarterly. Items that were green at launch can rot.

---

## Infrastructure

- [ ] Postgres is the storage backend (not SQLite). SQLite is fine for
      development and small single-node deployments only.
- [ ] Postgres backup configured with point-in-time restore (PITR).
      Recovery point objective (RPO) ≤ 5 minutes.
- [ ] Postgres replica configured + tested for read scaling.
- [ ] Object storage (S3/GCS/Azure Blob) backup configured for blobs.
      Test a full bucket restore once before launch.
- [ ] DNS records configured for each public-facing endpoint (gateway,
      dashboard).
- [ ] TLS certificates installed (Let's Encrypt or org PKI). Auto-renewal
      tested.
- [ ] Network policy / security groups locked down: only the LB → service
      port is open; service → DB port is restricted to the cluster.
- [ ] Container image vulnerability scanning (Trivy, Snyk, etc.) wired
      into CI; release blocked on critical CVEs.
- [ ] Image registry uses immutable tags; `latest` is forbidden in prod.
- [ ] Resource limits + requests set on every pod/container; no service
      can OOM-kill its node.

## Configuration

- [ ] `PLINTH_IDENTITY_JWT_SECRET` set explicitly (NOT auto-generated in
      prod). Stored in vault, rotated quarterly.
- [ ] `PLINTH_IDENTITY_JWT_ALG=RS256` for any deployment with > 1
      identity replica (HS256 doesn't support replicas without a shared
      secret, which is operationally awkward).
- [ ] `PLINTH_IDENTITY_KEYS_ENCRYPTION_KEY` set explicitly when using
      RS256. Stored in vault.
- [ ] `PLINTH_OAUTH_ENCRYPTION_KEY` set explicitly. Stored in vault.
- [ ] OAuth provider client credentials in vault, NOT in config files.
- [ ] `PLINTH_AUTH_REQUIRED=true` on workspace + gateway.
- [ ] `PLINTH_AUTH_MODE=verify_local` (or `verify_remote`) — never
      `permissive` in prod.
- [ ] `PLINTH_QUOTAS_ENABLED=true` on workspace + identity.
- [ ] `PLINTH_LOAD_SHED_ENABLED=true` with `max_inflight` tuned to
      ~80% of measured peak concurrency.
- [ ] `PLINTH_REGION_ID` set on every service (even single-region; lets
      future migration to multi-region work cleanly).
- [ ] `PLINTH_AUDIT_RETENTION_DAYS` aligned with the legal retention
      policy.
- [ ] `PLINTH_OTLP_ENABLED=true` and `PLINTH_OTLP_ENDPOINT` pointing at
      the OTel collector.

## Observability

- [ ] Prometheus is scraping `/metrics` on each service every 15s.
- [ ] OTLP collector is receiving gateway audit logs (verify with
      `GET /v1/observability/status` showing non-zero `events_emitted`).
- [ ] Grafana dashboards imported (JSONs in `deploy/grafana/`).
- [ ] Alerting rules configured (samples in
      `deploy/prometheus/alerts/`). On-call rotation receives test page.
- [ ] Log retention policy set (≥ 90 days for audit + compliance,
      ≥ 30 days for app logs).
- [ ] Log volume budgeted with the log vendor (Datadog, Honeycomb, etc.).
- [ ] Trace sampling configured at the OTel collector (Plynf itself
      doesn't sample).

## Security

- [ ] Tamper-evident audit chain enabled (column present, `prev_hash`
      populated for new rows).
- [ ] Audit chain verified daily by cron (`POST /v1/audit/verify`).
      `plinth_audit_chain_verified == 1` alert configured.
- [ ] OAuth tokens encrypted at rest verified (decrypt-and-re-encrypt
      smoke test).
- [ ] JWT signing key rotation cron tested (`POST /v1/keys/rotate`).
- [ ] Penetration test passed (recommended: external firm, annually).
- [ ] Threat model reviewed against deployment topology.
- [ ] Secrets are NOT in environment variables of running pods (use
      Kubernetes Secrets / sealed-secrets / vault-injector).
- [ ] CORS settings reviewed for the dashboard and gateway. Default-
      deny unless an explicit origin needs to be allowed.
- [ ] Rate limits tuned per tenant (defaults are intentionally generous;
      tighten for production traffic).

## Operations

- [ ] Runbooks written for each named SLO violation (see `docs/slos.md`
      for the canonical list).
- [ ] Runbooks for: outage, data corruption, key rotation, OAuth provider
      compromise, audit-chain break.
- [ ] On-call rotation defined; primary + secondary; PagerDuty (or
      equivalent) configured.
- [ ] Incident response process documented and rehearsed.
- [ ] Rollback procedure tested end-to-end. (Deploy v(N), then immediately
      rollback to v(N−1); confirm SLOs hold.)
- [ ] Disaster recovery plan tested. Specifically: restore Postgres from
      backup into a clean cluster, verify all services come up.
- [ ] Cost-cap monitoring in place: `plinth_tool_invocation_cost_usd_total`
      vs. budget; alert on >80% utilisation.
- [ ] Maintenance window communication channel defined (status page, etc.).
- [ ] Schema migration playbook tested (`POST /v1/admin/migrations/apply`
      on a staging snapshot of prod data).

## Compliance

- [ ] GDPR export per tenant tested (`GET /v1/compliance/tenants/{id}/export`).
- [ ] GDPR delete per tenant tested (`POST /v1/compliance/tenants/{id}/delete`).
- [ ] Data retention policies set per data class:
      - audit events ≥ 365 days
      - workspace KV/files: per tenant retention policy
      - logs ≥ 90 days
- [ ] Privacy notice deployed at the public website.
- [ ] DPA (Data Processing Agreement) signed with every sub-processor
      (your cloud, Datadog/Honeycomb, OAuth providers).
- [ ] Data residency requirements satisfied (PLINTH_REGION_ID + region
      pinning if EU customers).

## Performance

- [ ] Load test run against production-shape data; numbers within SLO
      targets (see `docs/slos.md`).
- [ ] Stress test for graceful degradation: load shedder kicks in at
      configured threshold, returns 503 with `Retry-After`, recovers
      cleanly when load drops.
- [ ] Capacity planning documented: peak QPS, peak storage growth/day,
      headroom multiplier (default 2×).
- [ ] Database connection pool sizes tuned per service.
- [ ] Cache hit ratio measured at steady state (> 30% target).

## Approvals

- [ ] Security review signed off (security team + SRE lead).
- [ ] Architecture review signed off (engineering lead).
- [ ] Legal/compliance signed off (privacy + DPA).
- [ ] Customer success notified (so support can prepare).
- [ ] Status page configured + owners listed.

---

## Sign-off

Launch is gated on signatures from at least three of:

* SRE Lead
* Engineering Lead
* Security
* Legal/Compliance

Record signatures in the launch ticket; the date is the official
go-live date for SLO measurement.
