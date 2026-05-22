# plinth-dashboard

The **Dashboard service** for [Plynf](../../README.md) — a small
FastAPI service that serves a single-page, read-only HTML/JS app showing
workspaces, channels, workflows, audit log, and cost rollups by polling
the workspace + gateway services.

This is what you point demos at: "show me what the agent infrastructure
looks like" → http://localhost:7424.

## What it shows

1. **Stat tiles** — workspaces, registered tools, total tool calls, cost
2. **Service health** — workspace / gateway / mock-mcp with version
3. **Workspaces table** — with a `view` link into a per-workspace page
4. **Tool calls** — recent 50 from the audit log
   (cached / duration / cost / audit id)
5. **Cost-by-tool bars** — USD per tool from the audit stats rollup
6. **Workspace detail** (sub-route) — KV entries, snapshots, channels,
   workflows

The dashboard is **read-only**. No mutations.

## Architecture

- **Backend**: FastAPI on port **7424**.
  - `GET /`                          → serves the SPA shell
  - `GET /workspaces/{ws_id}`        → also serves the SPA shell (sub-route)
  - `GET /static/{path}`             → CSS / JS / favicon
  - `GET /api/overview`              → aggregated JSON across services
  - `GET /api/workspaces[/...]`      → proxy to workspace service
  - `GET /api/audit`, `/api/audit-stats`, `/api/cache-stats`, `/api/tools`
                                     → proxy to gateway service
  - `GET /healthz`                   → liveness
- **Frontend**: vanilla HTML + JS + CSS, no build step. Hash-based
  router, 5s polling on the overview page.
- The aggregator fans out to workspace + gateway in parallel with
  `httpx.AsyncClient` + `asyncio.gather`. Any backend failure degrades
  gracefully — the response carries `partial: true`.

## Quickstart

```bash
# from the plinth repo root, using the shared venv
/Users/nico/Code/plinth/.venv/bin/pip install --ignore-requires-python -e ".[dev]"
/Users/nico/Code/plinth/.venv/bin/pytest -q --tb=short

# run the server (defaults are fine for local dev)
PLINTH_DASHBOARD_PORT=7424 python -m plinth_dashboard
# → uvicorn on http://0.0.0.0:7424

# health check
curl -s http://localhost:7424/healthz
# {"status":"ok","version":"0.1.0","service":"dashboard"}

# the dashboard payload
curl -s http://localhost:7424/api/overview | python3 -m json.tool | head -40
```

Open http://localhost:7424 in a browser. It auto-refreshes every five
seconds; click **Refresh** to force a pull or toggle auto-refresh off.

## `/api/overview` shape

```json
{
  "services": {
    "workspace": {"status": "up", "version": "0.1.0", "url": "http://localhost:7421"},
    "gateway":   {"status": "up", "version": "0.1.0", "url": "http://localhost:7422"},
    "mock_mcp":  {"status": "up", "version": "0.1.0", "url": "http://localhost:7423"}
  },
  "workspaces": {
    "count": 4,
    "list": [{"id": "...", "name": "...", "created_at": "..."}]
  },
  "audit": {
    "total_invocations": 142,
    "cached_count": 38,
    "error_count": 0,
    "total_cost_usd": 0.0234,
    "by_tool": [{"tool_id": "web.fetch", "count": 80, "cost": 0.0140}]
  },
  "cache": {"hits": 38, "misses": 104, "entries": 67, "size_bytes": 412341},
  "tools": {"count": 6},
  "partial": false,
  "fetched_at": "2026-05-06T12:34:56Z"
}
```

## Configuration

All settings come from environment variables prefixed with
`PLINTH_DASHBOARD_`:

| Var | Default | Purpose |
|-----|---------|---------|
| `PLINTH_DASHBOARD_PORT` | `7424` | uvicorn port |
| `PLINTH_DASHBOARD_HOST` | `0.0.0.0` | bind address |
| `PLINTH_DASHBOARD_WORKSPACE_URL` | `http://localhost:7421` | workspace base URL |
| `PLINTH_DASHBOARD_GATEWAY_URL` | `http://localhost:7422` | gateway base URL |
| `PLINTH_DASHBOARD_MOCK_MCP_URL` | `http://localhost:7423` | mock-mcp base URL (health-only) |
| `PLINTH_DASHBOARD_API_TOKEN` | `dashboard-token` | bearer token forwarded to backends |
| `PLINTH_DASHBOARD_LOG_LEVEL` | `INFO` | log level |
| `PLINTH_DASHBOARD_LOG_FORMAT` | `console` | `console` or `json` |

## Tests

```bash
pytest -q --cov=src/plinth_dashboard --cov-report=term-missing
```

Tests use `respx` to mock outbound httpx calls — no network access is
required.

## Docker

```bash
docker build -t plinth-dashboard .
docker run --rm -p 7424:7424 \
  -e PLINTH_DASHBOARD_WORKSPACE_URL=http://host.docker.internal:7421 \
  -e PLINTH_DASHBOARD_GATEWAY_URL=http://host.docker.internal:7422 \
  plinth-dashboard
```
