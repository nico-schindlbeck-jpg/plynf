---
title: API Stability
description: The v1 contract — what's guaranteed, what's allowed to change, and how v2 will work.
section: api
order: 1
sourceFile: docs/API_STABILITY.md
---

> **Status**: Active as of v1.0.0 GA. Audience: SDK consumers, integrators, and operators planning long-lived deployments.

## TL;DR

All endpoints under `/v1/...` and all SDK surfaces present at v1.0.0 are guaranteed backwards-compatible until v2.0.0 ships. We will not remove endpoints, remove required request fields, remove response fields, or change the semantic meaning of existing fields within v1. New v1 versions only add.

## Stability promise

For every service that exposes a `/v1/` API (Workspace, Gateway, Identity, Dashboard, the bundled MCP servers), the surface frozen at the v1.0.0 release tag is the contract. Through the entire v1 lifecycle:

- **No removed endpoints.** A path that resolved on v1.0.0 keeps resolving on every subsequent v1.x release.
- **No removed required request fields.** A request body or query parameter that was required on v1.0.0 stays required (or becomes optional with a default — but never disappears).
- **No removed response fields.** A field present in a successful response on v1.0.0 keeps being emitted on every later v1.x release.
- **No changed semantics.** A field's meaning, units, and value range are fixed by the v1.0.0 spec. We will not silently switch `cost_usd` from dollars to cents, or `created_at` from RFC 3339 to epoch milliseconds.
- **No tightened validation.** A request body accepted by v1.0.0 is accepted by every later v1.x release.
- **No reordered enum semantics.** Existing enum values keep their meaning; we may add new values but never reuse retired ones.

The promise applies to the JSON shapes documented in the OpenAPI specs at `specs/openapi/`. It is enforced by the contract test suite under `tests/contract/`, which runs in CI on every PR.

## What is allowed within v1

The promise is one-directional: clients written against v1.0.0 keep working. The server is free to change in additive, backwards-compatible ways. Specifically, any v1.x release MAY:

- Add new endpoints under `/v1/`.
- Add new optional request fields.
- Add new response fields (well-behaved clients tolerate unknown keys).
- Add new error codes inside the existing error envelope.
- Add new HTTP status codes for existing endpoints (only as a strict subset of "things the documented error envelope already permits").
- Add new OAuth providers, MCP integrations, and audit-event kinds.
- Change internal storage layouts, query plans, caching, rate-limit algorithms, and any other implementation detail not visible on the wire.
- Improve performance, clarify error messages, refine retry-after behaviour.

## Deprecation policy

When something needs to go away, it does not vanish from a v1.x release. Instead:

1. **Announce in `CHANGELOG.md`.** Each deprecation lists the version that first emits the deprecation signal and the target removal version. Removal targets a future v2.x — never a v1.x.
2. **Emit deprecation headers** on the deprecated endpoint:
   ```
   Deprecation: true
   Sunset: Wed, 01 May 2027 00:00:00 GMT
   Link: <https://docs.plinth.dev/api/v2/migration>; rel="alternate"
   ```
3. **SDK warnings.** The Python and TypeScript SDKs emit a runtime warning the first time a deprecated endpoint is hit per process.
4. **Minimum 12 months.** No fewer than 12 months elapse between the first release that emits the deprecation signal and the first release that can stop emitting the endpoint.
5. **Out-of-band notice.** Each deprecation is also posted to the project's release notes feed.

A deprecation signal does **not** change the endpoint's behaviour. The endpoint continues to respond exactly as documented in v1.0.0 right up to the point it stops being emitted in v2.

## How v2 will work

When the v2 line opens:

- v2 endpoints live at `/v2/...` parallel to `/v1`. The two are served from the same processes; routing is by path prefix.
- v1 is fully supported for at least **24 months** after v2 GA.
- Each removed-or-changed v1 endpoint has a migration guide in `docs/api/v2/migration.md` keyed by operationId.
- The v2 OpenAPI specs ship at `specs/openapi/<service>-v2.yaml`.
- During the v1+v2 overlap, both contract test suites run in CI.
- After v1 is retired, v1 paths are removed in a v2.x.0 minor release (not a patch).

## Scope

**In scope:**
- REST endpoints under `/v1/`.
- Request bodies, response bodies, query parameters, headers, status codes.
- The OpenAPI specs in `specs/openapi/`.
- The Python SDK surface (`plinth` package public API).
- The TypeScript SDK surface (`@plinth/sdk` package public API).
- The CLI surface (`plinth` command).

**Out of scope:**
- Internal storage formats.
- Internal service-to-service protocols (these may evolve freely).
- Performance characteristics (only direction of change is constrained).
