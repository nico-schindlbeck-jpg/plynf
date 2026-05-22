# plinth-gateway

The Tool Gateway service for [Plynf](https://github.com/plinth) — an HTTP proxy that
sits between AI agents and external tools (MCP servers, REST APIs).

## Features

- Tool registry: register HTTP/MCP-style tools with input/output schemas, auth, caching policy.
- Invocation proxy: call registered tools through a single `/v1/invoke` endpoint.
- Caching: SHA256 cache key over `tool_id + canonical_json(args)`, per-tool TTL.
- Audit log: every invocation captured with hashes, duration, cost estimate.
- Dry-run: simulate an invocation without calling the backend (cache hit returns result).
- Mock OAuth pass-through: `bearer` and `oauth2` (mock) auth for outbound calls.
- Rate limiting + cost caps (v0.2): per-`agent_id` (or `workspace_id`) token-bucket
  rate limit and rolling-window USD caps, enforced before every `/v1/invoke`.

## Quickstart

```bash
cd services/gateway
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run
PLINTH_DATA_DIR=/tmp/plinth-data python -m plinth_gateway

# Health
curl -s http://localhost:7422/healthz
```

## Configuration

All knobs are env-driven under the `PLINTH_` prefix.

| var | default | meaning |
|-----|---------|---------|
| `PLINTH_DATA_DIR` | `/tmp/plinth-data` | base dir for SQLite |
| `PLINTH_GATEWAY_PORT` | `7422` | bind port |
| `PLINTH_GATEWAY_HOST` | `0.0.0.0` | bind host |
| `PLINTH_LOG_LEVEL` | `INFO` | log level |
| `PLINTH_LOG_FORMAT` | `console` | `console` or `json` |
| `PLINTH_BACKEND_TIMEOUT_SECONDS` | `30` | outbound httpx timeout |
| `PLINTH_RATE_LIMIT_DEFAULT_RPM` | `60` | default per-agent calls/minute |
| `PLINTH_RATE_LIMIT_DEFAULT_BURST` | `20` | default per-agent burst capacity |
| `PLINTH_COST_CAP_DEFAULT_USD_HOUR` | `1.0` | default per-agent rolling 1-hour cap (USD; 0 disables) |
| `PLINTH_COST_CAP_DEFAULT_USD_DAY` | `10.0` | default per-agent rolling 24-hour cap (USD; 0 disables) |

## API

See `CONTRACTS.md` (Gateway API section) for the wire contract. The headline routes:

- `POST /v1/tools/register` — register a tool
- `GET  /v1/tools` — list tools
- `GET  /v1/tools/{tool_id}` — fetch one
- `DELETE /v1/tools/{tool_id}` — deregister
- `POST /v1/invoke` — call a tool (caching + audit)
- `POST /v1/invoke/dry-run` — simulate
- `GET  /v1/audit` — query the audit log
- `GET  /v1/audit/stats` — aggregates
- `GET  /v1/cache/stats` — cache hit/miss stats
- `DELETE /v1/cache?tool_id=` — clear cache (all or per tool)
- `GET  /v1/limits/{agent_id}` — fetch rate + cost-cap config (defaults if no override)
- `POST /v1/limits/{agent_id}` — upsert per-agent override
- `DELETE /v1/limits/{agent_id}` — revert to defaults
- `GET  /v1/limits/{agent_id}/status` — current usage vs caps
- `GET  /healthz` — liveness

## Rate limiting & cost caps

Every `POST /v1/invoke` consumes one token from a per-`agent_id` bucket
(falling back to `workspace_id`, then to a literal `"anonymous"` key) and
checks the rolling-window cost. If either limit is breached the gateway
returns:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 12
Content-Type: application/json

{"error": {
  "code": "RATE_LIMITED",                      // or COST_CAP_EXCEEDED
  "message": "Rate limit exceeded (rpm)…",
  "details": {"limit_type": "rpm", "retry_after_seconds": 12,
              "current": 60, "limit": 60}
}}
```

Cached calls cost $0 and never count toward the cost cap. Setting
`cost_cap_usd_hour: 0` (or `cost_cap_usd_day: 0`) disables that cap.

## Testing

```bash
pytest -v --cov=plinth_gateway --cov-report=term-missing
```

Tests use `httpx.AsyncClient` against the FastAPI app and `respx` to mock outbound HTTP.
No real network is hit.

## License

Apache-2.0.
