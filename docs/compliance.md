# Plynf Compliance Posture

> v1.0 baseline. Endpoints are the contractual surface operators
> integrate against; tests cover each mapping point.

Maps Plynf's v1.0 compliance scaffolding to SOC 2 Common Criteria,
GDPR Articles 17 / 20, and Article 32 security-of-processing. This
does **not** make Plynf "SOC 2 compliant" — compliance is an
organisational property — but gives an operator a defensible answer
to each control question.

## SOC 2 Common Criteria mapping

| CC ref | Topic | Plynf feature |
|---|---|---|
| CC1 / CC2 | Control environment + communication | Documented runtime in `OVERVIEW.md`, `ARCHITECTURE.md`, `CONTRACTS.md` |
| CC3 | Risk assessment | `docs/threat-model.md` |
| CC5 | Logical access controls | JWT capability tokens (`docs/architecture/06-identity-capabilities.md`), scope grammar enforcement, JTI revocation chain |
| CC6.1 | Logical access — user-level | `POST /v1/tokens` issues per-agent JWTs with deny-by-default scope; `/v1/tokens/{jti}/revoke` invalidates them; `/v1/revocations` propagates to peers |
| CC6.6 | System operations — boundary protection | TLS termination outside the trust boundary; per-tenant rate limits; load shedding |
| CC6.7 | Data transmission integrity | Hash-chained audit trail (`/v1/audit/verify`); request bodies hashed at audit time |
| CC7.1 | System monitoring | OTLP emitter forwards every audit event; `/v1/audit/stats` exposes aggregates |
| CC7.2 | Anomaly detection | `/v1/audit/verify` exposes any post-hoc tampering as `broken_at`; revocation stats track sudden spikes |
| CC8 | Change management | `migration_runner` checksums every applied SQL file; `verify_checksums` surfaces drift |
| CC9.2 | Vendor risk | OAuth tokens for third-party APIs are encrypted at rest with AES-256-GCM and never returned to API callers |

Many SOC 2 criteria (CC1 governance, CC4 monitoring committee, CC9.1
risk assessment cadence) live *outside* the software boundary —
operators supply them via runbooks, on-call rotations, and risk reviews.

## GDPR Article 20 — Right to data portability

The right to receive personal data "in a structured, commonly used and
machine-readable format" is implemented end-to-end as the export flow:

```
POST   /v1/tenants/{tenant_id}/export                         → 202 ExportJob
GET    /v1/tenants/{tenant_id}/exports/{export_id}            → 200 ExportStatus
GET    /v1/tenants/{tenant_id}/exports/{export_id}/download   → 200 application/zip
```

Identity orchestrates: it calls
`GET /v1/admin/tenant/{tenant_id}/export-data` on workspace and gateway
(each streams JSONL), adds its own JSONL, and bundles everything into a
ZIP at `$PLINTH_DATA_DIR/exports/<export_id>.zip` with a 24-hour
`expires_at`.

ZIP layout:
- `manifest.json` — version, export_id, tenant_id, timestamps, files
- `identity.jsonl` — tenants, tokens, quotas, usage
- `workspace.jsonl` — workspaces, kv/files, snapshots, branches,
  channels, channel_messages, workflows, workflow_steps, retention,
  resource_locks
- `gateway.jsonl` — tools, audit_events, agent_limits, oauth_connections
  (tokens redacted), oauth_states (PKCE redacted)

**Secrets are always redacted** — wrapped OAuth tokens carry the
literal `"REDACTED"` even though they're encrypted at rest.

## GDPR Article 17 — Right to erasure

Erasure is a two-phase confirm-then-cascade:

```
POST   /v1/tenants/{tenant_id}/delete-data-confirm  → 200 DeleteConfirmation
DELETE /v1/tenants/{tenant_id}/data?confirm=<tok>   → 202 DeleteJob
GET    /v1/tenants/{tenant_id}/delete-jobs/{id}     → 200 DeleteJob
```

Phase 1 mints a one-shot `confirm_token` (10 min TTL). Phase 2 consumes
it and runs the cascade:

1. Workspace `DELETE /v1/admin/tenant/{id}/data` — channel children →
   channels, kv_entries, file_entries, branches, snapshots, workflow
   leases/steps/parents, retention_policies, resource_locks, then
   workspaces. Blob files in `$DATA_DIR/blobs/<workspace>/` removed
   best-effort.
2. Gateway `DELETE /v1/admin/tenant/{id}/data` — audit_events,
   agent_limits, oauth_connections, oauth_states, tools. Cache wiped
   wholesale (cache rows aren't tenant-keyed).
3. Identity deletes own rows: issued_tokens, tenant_quotas,
   tenant_usage, then the tenants row. The literal `"default"` tenant
   is preserved — removing it would brick downstream services.

`DeleteJob.deleted_counts` records exactly what was removed per table.
Partial failure (e.g. workspace unreachable) still completes downstream
steps and surfaces the error in `deleted_counts`.

## GDPR Article 32 — Security of processing

| Article 32 control | Plynf feature |
|---|---|
| Encryption at rest of personal data | OAuth tokens AES-256-GCM (`encryption.py`); RSA private keys AES-256-GCM (`keys.py`) |
| Pseudonymisation | Audit `arguments_hash` + `result_hash` are sha256 — operators can store hashes without bodies |
| Confidentiality | Scope-based access control, tenant isolation, audit redaction of secrets |
| Integrity | Tamper-evident audit chain (`event_hash`/`prev_hash`), migration checksums |
| Availability | Per-tenant quotas, rate limits, load shedding, replication scaffolding |
| Resilience | Workflow lease reaper, idempotent migrations with rollback files |
| Restoration of access | Snapshots + branches in workspace; revocation cache replay from identity |
| Regular testing | `audit/verify` runs the chain check on demand; migration `verify_checksums` runs on each boot |

## Audit retention

The default retention policy is **forever** — `audit_events` rows are
never auto-pruned. Operators with regulatory retention requirements
should pair this with a periodic export job:

```
plinth audit --tenant <id> --since 90d --output audit-90d.jsonl
```

(or use the SDK's `client.tools.audit(since="90d")`). For legal-hold
contexts, forward audit events through the OTLP emitter to an
append-only store such as S3 with Object Lock or BigQuery's streaming
insert ingest path.

## Data residency

v1.0 ships **multi-region scaffolding**, not multi-region routing. The
relevant settings:

```
PLINTH_REGION_ID=eu-west-1
PLINTH_REGION_PEERS=us-east-1,ap-south-1
PLINTH_REPLICATION_MODE=primary|replica|standalone
```

Operators pin the data plane to a region by deploying Plynf services
into that region only, with `PLINTH_REPLICATION_MODE=standalone`. The
SQLite/Postgres database stays in-region by virtue of the deployment
topology; cross-region traffic only happens when the operator opts in.
The `GET /v1/regions` endpoint reports the configured region + peers
so a client can verify residency at runtime.

For multi-tenant deployments where each tenant needs its own residency
guarantee, the recommended pattern is **one Plynf deployment per
residency zone** — a single Plynf cluster does not currently shard
storage by tenant region.
