# Plinth — API v1 Stability Promise

> **Status**: Active as of v1.0.0 GA.
> **Owner**: Plinth maintainers.
> **Audience**: SDK consumers, integrators, operators planning long-lived
> deployments, and anyone reasoning about upgrade risk.

## TL;DR

All endpoints under `/v1/...` and all SDK surfaces present at v1.0.0 are
guaranteed backwards-compatible until v2.0.0 ships. We will not remove
endpoints, remove required request fields, remove response fields, or change
the semantic meaning of existing fields within v1. New v1 versions only add.

## Stability promise

For every service that exposes a `/v1/` API (Workspace, Gateway, Identity,
Dashboard, the bundled MCP servers), the surface frozen at the v1.0.0 release
tag is the contract. Through the entire v1 lifecycle:

- **No removed endpoints.** A path that resolved on v1.0.0 keeps resolving
  on every subsequent v1.x release.
- **No removed required request fields.** A request body or query parameter
  that was required on v1.0.0 stays required (or becomes optional with a
  default — but never disappears).
- **No removed response fields.** A field present in a successful response on
  v1.0.0 keeps being emitted on every later v1.x release.
- **No changed semantics.** A field's meaning, units, and value range are
  fixed by the v1.0.0 spec. We will not silently switch `cost_usd` from
  dollars to cents, or `created_at` from RFC 3339 to epoch milliseconds.
- **No tightened validation.** A request body accepted by v1.0.0 is accepted
  by every later v1.x release. We will not start rejecting previously-valid
  inputs.
- **No reordered enum semantics.** Existing enum values keep their meaning;
  we may add new values but never reuse retired ones.

The stability promise applies to the JSON shapes documented in the OpenAPI
specs at `specs/openapi/`. It is enforced by the contract test suite under
`tests/contract/`, which runs in CI on every PR.

## What is allowed within v1

The promise above is one-directional: clients written against v1.0.0 keep
working. The server is free to change in additive, backwards-compatible
ways. Specifically, any v1.x release MAY:

- Add new endpoints under `/v1/`.
- Add new optional request fields (clients must not send them on v1.0.0
  servers, but v1.0.0 clients are unaffected).
- Add new response fields (well-behaved clients tolerate unknown keys; the
  Python and TypeScript SDKs do).
- Add new error codes inside the existing error envelope. Per CONTRACTS.md,
  clients tolerate unknown codes by treating them as the catch-all `error`.
- Add new HTTP status codes for existing endpoints (only as a strict subset
  of "things the documented error envelope already permits").
- Add new OAuth providers, new MCP integrations, and new audit-event kinds.
- Change internal storage layouts, query plans, caching, rate-limit
  algorithms, and any other implementation detail not visible on the wire.
- Improve performance, clarify error messages, refine retry-after behaviour.

## Deprecation policy

When something needs to go away, it does not vanish from a v1.x release.
Instead:

1. **Announce in `CHANGELOG.md`.** Each deprecation lists the version that
   first emits the deprecation signal and the target removal version.
   Removal targets a future v2.x — never a v1.x.
2. **Emit deprecation headers on the deprecated endpoint:**
   ```
   Deprecation: true
   Sunset: Wed, 01 May 2027 00:00:00 GMT
   Link: <https://docs.plinth.dev/api/v2/migration>; rel="alternate"
   ```
   The `Sunset` header uses RFC 1123 dates (RFC 8594). The `Link` header
   points at the v2 equivalent.
3. **SDK warnings.** The Python and TypeScript SDKs emit a runtime warning
   the first time a deprecated endpoint is hit per process. The warning
   includes the sunset date and the migration URL.
4. **Minimum 12 months.** No fewer than 12 months elapse between the first
   release that emits the deprecation signal and the first release that can
   stop emitting the endpoint (which is always a v2.x release).
5. **Out-of-band notice.** Each deprecation is also posted to the project's
   release notes feed and security mailing list at announce-time.

A deprecation signal does **not** change the endpoint's behaviour. The
endpoint continues to respond exactly as documented in v1.0.0 right up to
the point it stops being emitted in v2.

## How v2 will work

When the v2 line opens:

- v2 endpoints live at `/v2/...` parallel to `/v1`. The two are served from
  the same processes; routing is by path prefix.
- v1 is fully supported for at least **24 months** after v2 GA. That gives
  integrators a full release cycle to plan, test, and migrate.
- Each removed-or-changed v1 endpoint has a migration guide in
  `docs/api/v2/migration.md` keyed by operationId. The `Link` deprecation
  header points at the per-endpoint anchor.
- The v2 OpenAPI specs ship at `specs/openapi/<service>-v2.yaml`. v1 specs
  remain at `specs/openapi/<service>.yaml` until v1 is retired.
- During the v1+v2 overlap, both contract test suites run in CI.
- After v1 is retired, the v1 paths are removed in a v2.x.0 minor release
  (not a patch release). The first v2.x release after retirement is
  explicitly called out in `CHANGELOG.md` as the breaking removal.

## Scope of this promise

**In scope:**

- REST endpoints under `/v1/`.
- Request bodies, response bodies, query parameters, headers, status codes.
- The OpenAPI specs in `specs/openapi/`.
- The Python SDK surface (`plinth` package public API).
- The TypeScript SDK surface (`@plinth/sdk` package public API).
- The CLI surface (`plinth` command, when present).

**Out of scope (explicitly):**

- Internal database schemas (SQLite or Postgres). Migrations may rewrite
  tables freely.
- Log line formats, log levels, log key names. These are operational
  diagnostics, not contract.
- Internal IPC between Plinth services (e.g. how Workspace and Gateway
  communicate). Treat these as private.
- Helm chart values structure. The chart has its own SemVer (`Chart.yaml`
  `version:`) and follows independent compatibility rules.
- The benchmarks / synthetic workloads under `benchmarks/`.
- Container image internals (base image choice, file layout, Python version
  etc.). The published `image: ghcr.io/.../plinth/<svc>:<tag>` reference
  with the documented env-var contract is what's stable.
- The Dashboard UI's HTML / JS. The dashboard is itself an API consumer; if
  you're scraping its rendered output, you're outside the contract.
- Pre-release endpoints under `/v1beta1/...` or `/v1alpha/...`. These are
  marked experimental in the OpenAPI specs and may change in any release.

## Versioning detail

Plinth uses standard SemVer for the project as a whole:

- `MAJOR` (e.g. v1 → v2): breaking changes to the v1 surface allowed.
- `MINOR` (e.g. v1.0 → v1.1): additive changes only. New endpoints, new
  optional fields, new response fields, new error codes.
- `PATCH` (e.g. v1.0.0 → v1.0.1): bug fixes. No spec changes.

The Helm chart, the SDK packages, and the CLI each carry their own SemVer
and are released independently.

## Header stability

Plinth services emit a small set of response headers that are part of the
v1 contract. Their **names and semantics** are frozen for v1; their values
are runtime-defined and may change request-to-request.

| Header | Emitted by | Meaning |
|--------|------------|---------|
| `X-Plinth-Request-Id` | every service | ULID for the request. Echoed in audit log entries and OTLP spans. |
| `X-Plinth-Tenant-Id` | every service when the bearer token resolves to a tenant | Tenant the request was authorized under. |
| `X-Plinth-Region` | every service when `PLINTH_REGION_ID` is set | Region that handled the request. |
| `X-Plinth-Cache` | gateway `/v1/invoke` | `hit` or `miss`. |
| `X-Plinth-Cost-Usd` | gateway `/v1/invoke` | Aggregated USD cost of the call (string-encoded decimal). |
| `Deprecation` | endpoints scheduled for removal | Always `true` when present. |
| `Sunset` | endpoints scheduled for removal | RFC 1123 date when the endpoint will stop being emitted. |
| `Link` | endpoints scheduled for removal | URL of the v2 equivalent / migration guide, with `rel="alternate"`. |

Standard HTTP request headers (`Authorization`, `Content-Type`, `Accept`,
`Idempotency-Key` on POSTs documented as idempotent) are interpreted exactly
as v1.0.0 documented them.

## Status code stability

Each documented `(operation, status)` pair on v1.0.0 is part of the contract.
The full table lives in `specs/openapi/<service>.yaml`; the contract test
suite asserts that every documented status code stays present in the
running service's OpenAPI output.

Concretely:

- `200`, `201`, `204` outcomes documented on v1.0.0 keep being emitted on
  the same conditions on every later v1.x release.
- `4xx` errors documented on v1.0.0 keep being emitted on the same
  conditions. We may add new `4xx` codes for new failure modes; we will not
  start emitting `4xx` where v1.0.0 returned `2xx`.
- `5xx` codes are operational and not part of the strict contract beyond
  the rule that the response body, when emitted, follows the documented
  error envelope (see below).

## Error code stability

The error envelope is fixed at v1.0.0:

```json
{
  "error": {
    "code": "rate_limited",
    "message": "Rate limit exceeded for tenant tnt_abc.",
    "request_id": "01HX...",
    "details": { "retry_after_seconds": 30 }
  }
}
```

- `error.code` is one of the values listed in `CONTRACTS.md` ("Error Model")
  for v1.0.0. Codes are frozen: clients can pattern-match on them.
- `error.message` is human-readable and **not** stable — wording may
  improve between releases.
- `error.request_id` matches `X-Plinth-Request-Id`.
- `error.details` is an open object whose keys we may extend additively.

Currently frozen codes (per CONTRACTS.md):

```
unauthorized          forbidden          not_found
conflict              rate_limited       cost_limit_exceeded
quota_exceeded        validation_error   internal_error
upstream_unavailable  timeout            tool_disabled
```

We may add new codes within v1; clients that don't recognize a code MUST
treat it as a generic `internal_error` per the SDK reference implementation.

## Endpoint coverage of this promise

The promise covers every documented operation. Operations are documented
only if they live under one of:

- `/v1/...` on Workspace, Gateway, Identity, Dashboard, or any bundled MCP.
- The `/healthz` and `/metrics` probes.

These prefixes are explicitly **out of contract**:

- `/v1/admin/...` — operator-only, not part of the agent surface.
- `/v1/internal/...` — service-to-service hops; subject to change.
- Anything under `/v1beta1/...` or `/v1alpha/...` — experimental opt-ins.
- The Dashboard SPA's `/api/...` routes — they are an internal consumer of
  the `/v1/` surface and not stable for third-party use.
- Wire format of cached responses on disk (the gateway's SQLite cache):
  treated as private implementation detail.

## What changed in v1.0 (vs v0.x)

The promise above takes effect at the v1.0.0 GA tag. Compared to the v0.6
release line, v1.0.0 also locks in:

- **Multi-region scaffolding (additive).** `PLINTH_REGION_ID`,
  `PLINTH_REPLICATION_MODE`, `PLINTH_REGION_PEERS` env vars and the
  `X-Plinth-Region` header are now part of the v1 contract.
- **OAuth flow endpoints (additive).** `/v1/oauth/{provider}/start`,
  `/v1/oauth/{provider}/callback`, `/v1/oauth/connections` on Gateway are
  promoted from v0.4 to v1 and frozen.
- **Capability tokens.** `Authorization: Bearer <jwt>` JWTs signed by the
  Identity service are the canonical token form. The legacy "any non-empty
  string" PoC token is still accepted for development but emits a
  `Deprecation: true` header on every request and will be removed in v2.0.
- **Error envelope.** Promoted from prose-only ("CONTRACTS.md → Error
  Model") to a tested contract enforced by `tests/contract/`.
- **Per-tenant quotas.** New `quota_exceeded` error code; existing tenants
  are auto-assigned generous defaults so v0.6 workloads pass through
  unchanged.
- **Workflow + channel APIs.** Locked at the surface frozen in v0.5 / v0.6.
- **Audit chain endpoint.** `/v1/audit/chain/verify` (Gateway) is now
  part of the contract; the response shape was finalized in v0.6.

No v0.x endpoint was removed for v1.0.0. The `Deprecation` header started
appearing in v1.0.0 only on the legacy non-JWT token flow described above.

## Reporting concerns

If you believe a v1.x release violates this promise, file an issue tagged
`api-stability`. Regressions are handled as security-class incidents and
patched in the next patch release.

## See also

- `CONTRACTS.md` — the prose source of truth for the API surface.
- `specs/openapi/` — the formal OpenAPI documents.
- `tests/contract/` — the running contract test suite.
- `scripts/openapi_diff.py` — CLI breaking-change checker, used in CI.
- `docs/deployment.md` — operator handbook.
