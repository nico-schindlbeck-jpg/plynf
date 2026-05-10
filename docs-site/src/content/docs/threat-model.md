---
title: Threat Model
description: STRIDE analysis of Plinth services, the SDK surface, and external integration points.
section: operations
order: 3
sourceFile: docs/threat-model.md
---

> Status: v1.0 baseline. Updated whenever a new attacker class or mitigation lands.

This page applies STRIDE (Spoofing-Tampering-Repudiation-Information-Disclosure-Denial-Elevation) to Plinth's three core services (`workspace`, `gateway`, `identity`), the SDK surface, and external integration points (OAuth providers, MCP servers, OTLP collectors). The output is a catalogue of concrete threats, the v1.0 mitigations that exist for each, and the residual risks an operator inherits.

The adversary is assumed **on the network**: they can send arbitrary HTTP, observe TLS-terminated traffic at the load balancer, and — for the "compromised-agent" class — already hold a valid scoped JWT for one tenant.

## Scope

**In scope** for v1.0:

- The three Plinth services (`workspace`, `gateway`, `identity`) and the storage they own (SQLite/Postgres + on-disk blob trees).
- The SDK surface (Python + TypeScript) where it terminates a session against a Plinth deployment.
- External integration points where Plinth makes outbound calls (MCP servers, OAuth providers, OTLP collectors).
- The deployment topology described in `deploy/k8s/`, `deploy/helm/`, and `deploy/terraform/aws-example/`.

**Out of scope** for v1.0 (operator responsibility):

- Physical security of the host hardware.
- Underlying database engine vulnerabilities (CVEs in SQLite/Postgres).
- DDoS at the network/CDN layer — Plinth's load shedding is a last-resort, not an edge mitigation.
- Compromised SDK distribution (PyPI / npm supply chain attacks).
- Operator-supplied integrations (custom MCP servers, custom OAuth providers).

## Trust boundaries

```
            ┌────────────────────────────────────────────────┐
            │                  Untrusted Zone                │
            │  agents ─► HTTPS ─► [LB / TLS] ─► (cont.)      │
            └────────────────────────────────────────────────┘
                                     │
            ┌─────────────────────────────────────────────────┐
            │             Plinth Service Mesh                 │
            │  identity ◄────► gateway ◄────► workspace       │
            │     │              │              │             │
            │     ▼              ▼              ▼             │
            │  identity.db   gateway.db    workspace.db       │
            │                                + blobs/         │
            └─────────────────────────────────────────────────┘
                                     │
            ┌─────────────────────────────────────────────────┐
            │        Outbound (via gateway only)              │
            │  OAuth providers   MCP servers   OTLP collector │
            └─────────────────────────────────────────────────┘
```

Trust transitions:

1. **Agent ↔ service**: every cross-service call carries a JWT minted by `identity`. TLS termination is outside the trust boundary; every internal hop is authenticated by JWT, not by network position.
2. **Service ↔ service**: gateway/workspace verify JWTs against identity's published JWKS (RS256) or shared secret (HS256). Identity is the only party that can mint new tokens.
3. **Service ↔ database**: storage is inside the trust boundary on a single node. Clustered deployments must wire mTLS between replicas.
4. **Service ↔ external**: outbound HTTP from gateway only (proxy + OAuth client). Workspace and identity make no outbound HTTP except for region peer probes.

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

## Headline mitigations (per class)

| Class | Top mitigation |
|---|---|
| A | Rate limiting + load shedding at every service boundary; no anonymous endpoint mutates state |
| B | JWT scopes are deny-by-default and parsed to a strict grammar; per-tenant quotas |
| C | Tenant ID is encoded in every JWT and re-checked at each service; cross-tenant requests fail with 403 |
| D | All gateway responses are size-bounded; JSON is schema-validated; results hashed before audit |
| E | Tokens AES-256-GCM at rest; audit body hashes are SHA-256, not plaintext |
| F | Tamper-evident audit chain with `prev_hash` linking; daily `verify_checksums` cron |
| G | Identity-published revocation list with sub-second federated propagation; revocations cached at every peer |
| H | All inter-service hops require JWT auth; no implicit trust by network position |

For the full STRIDE walk-through with per-component mitigations and residual-risk notes, see `docs/threat-model.md` in the repo.
