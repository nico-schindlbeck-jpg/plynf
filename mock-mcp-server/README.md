# Mock MCP Server

A minimal MCP-style server that exposes 6 demo tools used by the Plinth
research-agent demo and other examples. Designed to run **fully offline**
with bundled fixture content, so anyone can clone the repo and exercise
the tool surface without network or API keys.

## Tools

| `tool_id` | Description |
|-----------|-------------|
| `web.fetch` | Fetch a URL. `mock://` URLs return canned fixture content; `https://` uses httpx (10 s timeout). |
| `web.search` | Mock web search. Returns canned results matched against the query topic; falls back to the renewable-energy fixtures for unknown queries. |
| `fs.read` | Read a UTF-8 text file from the fixtures directory (sandboxed; path traversal blocked). |
| `fs.write` | Write a UTF-8 text file under the fixtures directory (sandboxed). |
| `notes.add` | Append a note to the in-memory store. |
| `notes.list` | List all in-memory notes. |

The fixtures cover three topics — **renewable energy**, **ai agents**, and
**climate policy** — with five sources each (~1500 words apiece). The
content is byte-identical to the bundled fixtures in
`examples/01-research-agent/shared.py`, so the demo behaves the same
whether it pulls sources from this server or from its in-process bundle.

## Endpoints

```
GET  /healthz
GET  /tools
POST /invoke/{tool_name}
```

Errors follow the standard Plinth envelope:

```json
{"error": {"code": "INVALID_ARGUMENTS", "message": "...", "details": {}}}
```

## Run it

```bash
cd mock-mcp-server
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
PLINTH_MOCK_PORT=7423 python -m mock_mcp
```

In another shell:

```bash
curl -s http://localhost:7423/healthz
curl -s http://localhost:7423/tools | jq .
curl -s -X POST http://localhost:7423/invoke/web.fetch \
  -H 'content-type: application/json' \
  -d '{"url": "mock://renewable-energy-1"}' | jq .
```

## Configuration

All env vars are prefixed with `PLINTH_MOCK_`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `PLINTH_MOCK_PORT` | `7423` | TCP port for uvicorn |
| `PLINTH_MOCK_FIXTURES_DIR` | `/tmp/plinth-mock-fixtures` | Sandbox root for `fs.*` tools |
| `PLINTH_MOCK_LOG_LEVEL` | `INFO` | Standard logging level |
| `PLINTH_MOCK_LOG_FORMAT` | `console` | `console` or `json` |

## Tests

```bash
pip install -e ".[dev]"
pytest -v --cov=mock_mcp --cov-report=term-missing
```

Coverage target is >=80% on `src/`.

## Adding a tool

1. Add a handler and a `Tool` record in `src/mock_mcp/tools.py`. The handler
   is `async def handler(args: dict, ctx: ToolContext) -> dict`; raise
   `ToolError("CODE", "message")` for validation/lookup failures.
2. Append the new `Tool` to `TOOL_LIST` (this is the only place you need
   to wire it in — the registry and `/tools` endpoint pick it up
   automatically).
3. Add tests in `tests/test_tools.py`. Use `respx` if your handler talks
   to a real HTTP service so the tests stay offline.

## Docker

```bash
docker build -t plinth/mock-mcp:0.1.0 .
docker run --rm -p 7423:7423 plinth/mock-mcp:0.1.0
```

## License

Apache-2.0. See `LICENSE` at the repo root.
