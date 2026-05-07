# Flow — tool invocation through the gateway

This flow shows what happens inside the Tool Gateway when an agent calls
`POST /v1/invoke`. It mirrors the request flow in `ARCHITECTURE.md` and is the
contract every backend (HTTP, MCP stdio, etc.) plugs into.

The gateway is the single auth boundary, single audit boundary and single
cache boundary for all tool calls. Anything that bypasses this flow forfeits
those properties.

```mermaid
flowchart TD
    Start([POST /v1/invoke<br/>InvokeRequest]) --> Validate{Validate<br/>InvokeRequest}
    Validate -- invalid --> ErrInvalid[400 INVALID_ARGUMENTS]
    Validate -- valid --> Lookup[Lookup tool<br/>by tool_id]
    Lookup -- not found --> ErrNotFound[404 TOOL_NOT_FOUND]
    Lookup -- found --> Policy{Policy check<br/>capability scope?}
    Policy -- denied --> ErrAuth[401 UNAUTHORIZED]
    Policy -- allowed --> RateLimit{Rate limit<br/>OK?}
    RateLimit -- exceeded --> ErrRate[429 RATE_LIMITED]
    RateLimit -- ok --> CacheKey[Compute cache key<br/>sha256 tool_id ‖ canonical_json args]

    CacheKey --> CacheCheck{Cache enabled?<br/>tool.idempotent AND<br/>request.cache AND<br/>tool.cache_ttl_seconds}
    CacheCheck -- no --> Auth[Resolve credentials<br/>per tool.auth_method]
    CacheCheck -- yes --> CacheLookup{Cache lookup}
    CacheLookup -- hit & fresh --> RecordHit[Append AuditEvent<br/>cached=true]
    RecordHit --> RespondCached[200 InvokeResponse<br/>cached=true]
    CacheLookup -- miss / expired --> Auth

    Auth --> Backend{transport}
    Backend -- http --> CallHTTP[POST endpoint with creds]
    Backend -- stdio --> CallStdio[spawn / pipe stdio]
    CallHTTP --> Result[Result + duration]
    CallStdio --> Result

    Result --> ResultOk{Backend ok?}
    ResultOk -- no --> RecordError[Append AuditEvent<br/>error set, cached=false]
    RecordError --> ErrUpstream[500 TOOL_INVOCATION_FAILED]
    ResultOk -- yes --> RecordOk[Append AuditEvent<br/>arguments_hash, result_hash,<br/>duration, cost]

    RecordOk --> CacheStore{Idempotent AND<br/>cache enabled?}
    CacheStore -- yes --> Store[Store result in cache<br/>TTL = tool.cache_ttl_seconds]
    CacheStore -- no --> Done
    Store --> Done([200 InvokeResponse<br/>cached=false])

    classDef errNode fill:#ffe5e5,stroke:#a40000,color:#5a0000;
    classDef okNode fill:#e8f7ee,stroke:#137333,color:#0d4e1f;
    class ErrInvalid,ErrNotFound,ErrAuth,ErrRate,ErrUpstream errNode;
    class RespondCached,Done okNode;
```

## Cache key recipe

```
cache_key = sha256(
    utf8(tool_id) || 0x00 ||
    canonical_json(arguments)
)
```

`canonical_json` = JCS / RFC 8785 (sorted keys, no whitespace, normalised
numbers). This guarantees that semantically equal calls share a key.

## Audit event invariants

- Always emitted, including for cache hits and errors.
- `arguments_hash` and `result_hash` are SHA-256 over the canonical-JSON
  serialisations. The raw values are *not* persisted in the audit log.
- The audit row's `id` (`evt_<ulid>`) is returned to the caller as
  `InvokeResponse.audit_id` so SDKs can correlate logs.

## Dry-run difference

`POST /v1/invoke/dry-run` follows the same path through `CacheKey` and
`CacheLookup`, but returns `DryRunResponse` and never reaches the backend or
the audit append step.
