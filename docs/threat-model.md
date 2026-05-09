# Plinth Threat Model

> Status: v1.0 baseline. Updated whenever a new attacker class or
> mitigation lands. Section anchors are stable for citation from
> incident response runbooks.

This document applies STRIDE
(Spoofing-Tampering-Repudiation-Information-Disclosure-Denial-Elevation)
to Plinth's three core services (`workspace`, `gateway`, `identity`),
the SDK surface, and external integration points (OAuth providers, MCP
servers, OTLP collectors). The output is a catalogue of concrete
threats, the v1.0 mitigations that exist for each, and the residual
risks an operator inherits.

The adversary is assumed **on the network**: they can send arbitrary
HTTP, observe TLS-terminated traffic at the load balancer, and — for
the "compromised-agent" class — already hold a valid scoped JWT for
one tenant.

## Scope

**In scope** for v1.0:

- The three Plinth services (`workspace`, `gateway`, `identity`) and
  the storage they own (SQLite/Postgres + on-disk blob trees).
- The SDK surface (Python + TypeScript) where it terminates a session
  against a Plinth deployment.
- External integration points where Plinth makes outbound calls (MCP
  servers, OAuth providers, OTLP collectors).
- The deployment topology described in `deploy/k8s/`, `deploy/helm/`,
  and `deploy/terraform/aws-example/`.

**Out of scope** for v1.0 (operator responsibility):

- Physical security of the host hardware.
- Underlying database engine vulnerabilities (CVEs in SQLite/Postgres).
- DDoS at the network/CDN layer — Plinth's load shedding is a
  last-resort, not an edge mitigation.
- Compromised SDK distribution (PyPI / npm supply chain attacks).
- Operator-supplied integrations (custom MCP servers, custom OAuth
  providers) beyond the protocol-level controls Plinth enforces.

## Trust boundaries

```
            ┌────────────────────────────────────────────────┐
            │                  Untrusted Zone                │
            │                                                │
  agents ─► │── HTTPS ──► [LB / TLS termination] ─► (cont.)  │
   SDK ─►   │                                                │
            └────────────────────────────────────────────────┘
                                     │
            ┌─────────────────────────────────────────────────┐
            │             Plinth Service Mesh                 │
            │                                                 │
            │  identity ◄────► gateway ◄────► workspace       │
            │     │              │              │             │
            │     ▼              ▼              ▼             │
            │  identity.db   gateway.db    workspace.db       │
            │                                + blobs/         │
            └─────────────────────────────────────────────────┘
                                     │
            ┌─────────────────────────────────────────────────┐
            │        Outbound (via gateway only)              │
            │                                                 │
            │  OAuth providers   MCP servers   OTLP collector │
            └─────────────────────────────────────────────────┘
```

Trust transitions:

1. **Agent ↔ service**: every cross-service call carries a JWT minted
   by `identity`. TLS termination is outside the trust boundary; every
   internal hop is authenticated by JWT, not by network position.
2. **Service ↔ service**: gateway/workspace verify JWTs against
   identity's published JWKS (RS256) or shared secret (HS256). Identity
   is the only party that can mint new tokens.
3. **Service ↔ database**: storage is inside the trust boundary on a
   single node. Clustered deployments must wire mTLS between replicas.
4. **Service ↔ external**: outbound HTTP from gateway only (proxy +
   OAuth client). Workspace and identity make no outbound HTTP except
   for region peer probes.

## Attacker classes

| Class | Description | Capabilities |
|---|---|---|
| **A — Network anonymous** | No credentials, IP-only access | Send HTTP, observe error codes + response timing |
| **B — Compromised agent** | Holds a valid scoped JWT in tenant X | Anything the JWT scopes allow |
| **C — Cross-tenant operator** | Holds a valid token in tenant A | Tries to read/write tenant B data |
| **D — Compromised MCP server** | Operator-registered tool returns hostile content | Returns oversized payloads, malformed JSON, embedded prompt-injection |
| **E — Insider (read)** | Has DB read access (e.g. backups, replicas) | Reads SQLite files, OTLP exports |
| **F — Insider (write)** | Has DB write access | Mutates `audit_events`, `oauth_connections`, etc. in-place |
| **G — Stolen-token replay** | Captured an old, possibly revoked JWT | Replays it after revocation propagation lag |
| **H — Network-level attacker** | Passive eavesdropper or active MitM behind the LB | Drops, replays, reorders inter-service traffic |

## STRIDE analysis

### S — Spoofing

| ID | Threat | Mitigation |
|----|--------|------------|
| S1 | Forged JWT presented to gateway/workspace | RS256 signature verification against published JWKS; HS256 fallback uses HMAC; bad signatures fail with 401. See `gateway/jwt_auth.py`, `workspace/auth.py`. |
| S2 | Tenant impersonation by manipulating `tenant_id` claim | Tenant ID is read from the JWT claim, not from URL/body; per-tenant context middleware overwrites any body-supplied tenancy. |
| S3 | Replay of revoked JWT after compromise | JTI-based revocation via `/v1/tokens/{jti}/revoke`; downstream services poll `/v1/revocations` every 60s and update an in-memory blocklist. |
| S4 | Token minted by a different deployment accepted | Issuer + audience validation. Every decoder requires `iss == identity_url` and `aud == identity_jwt_audience`. |
| S5 | Agent impersonation when multiple agents share a single token | Documented operator policy: mint per-agent tokens; the audit log surfaces the `agent_id` claim so misuse is post-hoc detectable. |
| S6 | Spoofed identity service (rogue JWKS publisher) | Production deployments must front identity with TLS + a known certificate. The `identity_jwks_url` config is set at deploy time and not discoverable. |

**Residual risks**

- HS256 shares a secret across services; host compromise = secret
  compromise. Run RS256 in production and rotate keys via the
  background rotation loop or `/v1/keys/rotate`.
- Identity itself is unauthenticated for token issuance — operators
  must put it behind a private LB.
- Revocation cache lag (≤60s by default). Security-critical paths
  should consult `/v1/tokens/{jti}` synchronously rather than relying
  on the cached blocklist.

### T — Tampering

| ID | Threat | Mitigation |
|----|--------|------------|
| T1 | Audit log tampering — insider edits `arguments_hash` post-hoc | Tamper-evident hash chain (v1.0). Every `audit_events` row stores `prev_hash` + `event_hash = sha256(prev_hash || canonical_json(event))`. `GET /v1/audit/verify` walks the chain and reports the first divergent row. See `gateway/audit.py::verify_chain`. |
| T2 | Audit log tampering — drop trailing rows | Chain is append-only-*detectable*, not -enforced. Forward audit events through OTLP to S3 Object Lock or BigQuery for stronger guarantees. |
| T3 | Workspace KV poisoning by other tenant | Tenant isolation: every query is filtered by `tenant_id` from the JWT. Cross-tenant lookups return 404 (existence not leaked). |
| T4 | Result tampering — modify `result_hash` after the fact | Chain hash covers the `result_hash`, so editing it breaks `event_hash`. |
| T5 | Migration tampering — swap a SQL file post-apply | Migration runner re-hashes every applied SQL on boot; drift surfaces at `/v1/admin/migrations` as a `mismatches` entry. |
| T6 | Workspace version overwrite | Workspace versioning is immutable per `(workspace, key, branch)` — writes append, never UPDATE. Tampered values still leave the previous version visible. |
| T7 | OAuth state-parameter tampering for CSRF | Per-flow `oauth_states` row with PKCE verifier; the state value is opaque + single-use. |
| T8 | Configuration tampering at runtime | Settings are read from env at startup; runtime mutation requires service restart, which is an admin-bound operation. |

**Residual risks**

- Chain detects but does not prevent tampering. Defence-in-depth via
  external attestation (planned: daily Merkle root publication).
- SQLite WAL pre-images can leak post-overwrite data on disk.
  Postgres sidesteps this; for SQLite deployments operators should
  encrypt the volume.

### R — Repudiation

| ID | Threat | Mitigation |
|----|--------|------------|
| R1 | Agent denies ever invoking a tool | Every invocation is audited with `agent_id`, `workspace_id`, `tenant_id`, timestamp, arguments hash, result hash, duration, cost. Cache hits are recorded with `cached=1`; audit is unconditional. |
| R2 | Operator denies issuing a token | RS256 tokens are non-repudiable — only identity's private key could have signed them. JWT itself is not stored, but `issued_tokens` carries the metadata + JTI. |
| R3 | Worker denies executing a workflow step | Workflow steps carry `attempt` + `worker_id` so retries are distinguishable from the original execution; lease records pin the worker for the lease duration. |
| R4 | Cross-tenant access denied — "I never tried" | Failed authz attempts are logged at WARNING level with the `agent_id` from the JWT and the path attempted; operators correlate via `request_id`. |

**Residual risks**

- `agent_id` is only as strong as the issuing flow. Mint per-agent
  tokens; never share one across agents.
- Pre-v1.0 audit rows have NULL chain columns and aren't part of the
  verifiable chain. Rebuilding requires offline migration; the
  verification endpoint reports them as "skipped" so operators can
  detect the boundary.

### I — Information disclosure

| ID | Threat | Mitigation |
|----|--------|------------|
| I1 | Cross-tenant data leak via direct workspace ID guess | Tenant isolation. Lookups filter by `tenant_id` from the JWT; cross-tenant fetches return 404 (not 401) so existence is not leaked. |
| I2 | OAuth token in logs | OAuth access/refresh tokens are AES-256-GCM encrypted at rest (`encryption.py`). Logs only carry the `connection_id`, never the token bytes. |
| I3 | OAuth token in GDPR export | Export redacts `access_token_encrypted`, `refresh_token_encrypted`, and `pkce_verifier` to the literal string `"REDACTED"`. The export goes to the user's hands; encrypted blobs are useless to them and a defence-in-depth no-no. |
| I4 | Audit `arguments` body in cleartext | Audit stores `arguments_hash` + a 500-char preview, not the full body. Operators with retention duties can reconstruct via OTLP forwarding to a stricter store. |
| I5 | DB backup leak exposes wrapped tokens | Tokens are encrypted with a key the operator controls; an attacker holding the DB without the key still cannot decrypt. Operator must rotate `oauth_encryption_key` separately. |
| I6 | RSA private keys in the keys table | Private PEMs are AES-256-GCM wrapped at rest using `identity_keys_encryption_key`. JWKS only publishes the public half. |
| I7 | `/healthz` leaks version information | Documented behaviour. Operators put `/healthz` behind a private LB if pinning version disclosure is a concern. |
| I8 | Cross-tenant cache poisoning leaks data | Cache is keyed by `tool_id + arguments_hash` and is **not tenant-keyed** in v1.0. Two tenants hitting an idempotent tool with identical args could serve each other's cached results. Mitigations: GDPR delete wipes cache wholesale; operators set `cache_ttl_seconds=0` on tools that carry tenant-distinguishing data. Per-tenant cache partitioning is planned for v1.1. |
| I9 | Side-channel via response timing on tenant existence | Mitigated by the 404-on-missing rule for cross-tenant lookups; timing comparisons across tenants still leak whether a tenant exists, which is a documented limitation. |
| I10 | Compromised MCP server reads workspace secrets | Tools receive only the arguments the gateway forwards; gateway does not mount a workspace-wide view into the tool. The operator decides what `arguments` shape includes. |

**Residual risks**

- Postgres-level data-at-rest encryption is the operator's
  responsibility (CloudSQL CMEK, RDS KMS, etc.).
- The `arguments_preview` is best-effort 500-char truncation; operators
  whose tools carry secrets in arguments must opt out via the audit
  filter list.

### D — Denial of service

| ID | Threat | Mitigation |
|----|--------|------------|
| D1 | Agent floods `POST /v1/invoke` | Per-agent token-bucket rate limit (default 60rpm/20burst) configurable via `POST /v1/limits/{agent_id}`. See `rate_limit.py`. |
| D2 | Agent runs up cost via expensive tool calls | Per-agent cost caps (rolling hour + day windows over `audit_events.cost_estimate_usd`). 429 + `Retry-After` on exceed. |
| D3 | Tenant flood — one tenant hogs the deployment | Per-tenant quotas (v1.0): `max_workspaces`, `max_storage_gb`, `max_invocations_per_minute`, `max_cost_usd_day`/`month`, enforced at resource-create and invoke time. |
| D4 | Slow-loris / queue exhaustion | Load shedding: outermost middleware rejects 503 + `Retry-After` when `inflight + queued > max`. |
| D5 | Concurrent migration writes corrupt schema | Migration locks (`fcntl` on SQLite, `pg_advisory_lock` on Postgres) prevent concurrent schema changes; second writer gets a `MIGRATION_LOCKED` 409. |
| D6 | Workflow-step lease leaks worker hours | Lease reaper sweeps expired leases on a periodic timer; abandoned leases are reclaimable. |
| D7 | OAuth refresh storm | Refresh attempts coalesce per `connection_id`; concurrent invokes wait on the in-flight refresh future rather than each kicking off a new HTTP request. |
| D8 | Audit-log unbounded growth | Default retention is "forever". Operators with capacity concerns deploy a retention policy via the workspace `retention_policies` table or forward to a colder store. |
| D9 | Channel queue overflow | Each channel has a `max_buffered` setting. Producers see a 429 when the buffer is full; the gateway emits a backpressure metric. |

**Residual risks**

- Cost caps depend on `pricing.py`. A tool with zero/unconfigured
  pricing contributes 0 — audit when registering paid integrations.
- DDoS at infra layer (volumetric L3/L4) is the operator's
  responsibility — Plinth's load shedding is a last-resort, not an
  edge mitigation.

### E — Elevation of privilege

| ID | Threat | Mitigation |
|----|--------|------------|
| E1 | Scope inflation via crafted JWT body | Scopes come from the signed claim, not from headers/body; `policy.check_capability` rejects calls without the required scope. |
| E2 | Admin endpoint misuse without admin scope | Admin endpoints (`/v1/admin/...`) require `*` or `tenant:*:admin`. Permissive (no auth required) is dev-only and gated by `PLINTH_AUTH_MODE=permissive`. |
| E3 | GDPR delete cascade triggered by stolen JWT | Two-phase delete: `POST .../delete-data-confirm` mints a one-shot token (10 min TTL); the actual `DELETE .../data?confirm=…` consumes it. A stolen admin JWT cannot wipe data in a single replayed request. |
| E4 | Replay of GDPR confirm-token after consumption | Tokens are deleted from `delete_confirm_tokens` on consume; second attempt fails with `DELETE_CONFIRM_INVALID`. |
| E5 | Workspace ID guess leaks privileged data | Workspace queries filter by `tenant_id` from the JWT; guesses across tenants 404. |
| E6 | OAuth token redirect to attacker's URL | Per-flow `redirect_uri` is registered at provider-config time; OAuth callback validates the URL against the registered list. |
| E7 | Service-to-service call from attacker bypasses scope | Identity-to-{workspace,gateway} admin calls require `PLINTH_AUTH_MODE=verify_local` in production. Permissive mode accepts the literal `Bearer compliance-orch` only in dev. |
| E8 | Privilege escalation through a workflow step's tool registration | Tool registration is a tenant-scoped operation; tools are isolated per tenant in the registry. A tenant-A tool cannot be invoked from tenant-B. |

**Residual risks**

- `*` scope is honoured everywhere — operators must rotate ops tokens
  aggressively. Mint short-TTL `*` per-incident, never long-lived
  break-glass.
- Identity's calls into workspace+gateway admin endpoints with a
  service bearer are only safe under `verify_local`; mis-configured
  permissive mode in production opens E7.

## Residual risks (v1.0)

The following risks remain unmitigated in v1.0 and are inherited by
the operator:

- **Postgres-level data-at-rest encryption**: operator responsibility
  via CloudSQL CMEK, RDS KMS, or equivalent.
- **DDoS at infra layer**: operator responsibility — front Plinth with
  a CDN / WAF / volumetric scrubber.
- **Compromised SDK distribution**: out of scope. Operators pin SDK
  versions and verify checksums.
- **Cache cross-tenant leakage**: see I8. Mitigated by per-tool
  `cache_ttl_seconds=0` for sensitive tools; full per-tenant cache
  partitioning is planned for v1.1.
- **Audit chain enforcement**: tamper-detection only, not prevention.
  Defence-in-depth is the operator's responsibility (S3 Object Lock,
  BigQuery streaming insert, etc.).
- **mTLS between services**: not enforced in v1.0. Operators in
  zero-trust environments must wrap inter-service traffic at the
  service-mesh layer (Istio, Linkerd) or via Tailscale/VPC tunnels.
- **Rate limits in standalone-replica mode**: per-agent buckets are
  per-instance. A multi-replica deployment without a shared rate
  limiter will see the bucket multiplied by the replica count.
  Operators in this topology should front Plinth with an edge rate
  limiter (Envoy, NGINX, Cloudflare).

## Future work

- **v1.1**: per-tenant cache partitioning, daily Merkle-root
  attestation of the audit chain, mTLS between identity and other
  services, shared rate-limit backplane (Redis/sliding-window).
- **v1.2**: Hardware security module (HSM) integration for the JWT
  signing key, tenant-scoped log retention policies enforced at the
  audit-log layer, encrypted-at-rest blobs (currently relies on disk
  encryption).
- **v2.0**: Confidential computing support (TEE/SGX) for tenants with
  strict data-residency + isolation requirements, optional Postgres
  row-level security profiles.

## Open work

- External attestation of the audit chain (daily Merkle root,
  publicly logged hash).
- Per-tenant cache partitioning.
- mTLS between identity and the other services.
- Shared rate-limit backplane for multi-replica deployments.
- Hardware security module (HSM) integration for the JWT signing key.
- Tenant-scoped log retention policies.
