# Plynf Asana MCP Server

A minimal MCP-style server that wraps the Asana REST API for use with
Plynf agents. Runs at `http://localhost:7433` by default.

## What it does

Exposes six tools that mirror the Asana REST surface most agents need:

| `tool_id`                  | Side effects | Notes                                |
| -------------------------- | ------------ | ------------------------------------ |
| `asana.list_workspaces`    | read         | All accessible workspaces            |
| `asana.list_projects`      | read         | Projects in a workspace              |
| `asana.list_tasks`         | read         | Tasks in a project                   |
| `asana.get_task`           | read         | Single task with project membership  |
| `asana.create_task`        | write        | Create a task in workspace/projects  |
| `asana.update_task`        | write        | PATCH name/notes/completed/assignee  |

Every tool advertises `auth_method=oauth2` with `auth_config.provider="asana"`.

## Auth

This server **never holds the Asana access token**. The gateway forwards the
user's bearer token via the `Authorization` header on each `POST /invoke/...`
call; the tools then forward the same token to Asana. If the header is
missing or unparseable, the server returns 401 with a Plynf error envelope.

## Endpoints

```
GET  /healthz              -> {"status":"ok","version":"1.5.0","service":"asana-mcp"}
GET  /tools                -> list of 6 tools with input/output schemas
POST /invoke/{tool_name}   body: args  -> {"result": ...} | {"error": {...}}
GET  /metrics              -> Prometheus exposition
```

## Run locally

```bash
cd mcp-servers/asana
pip install -e ".[dev]"
python -m asana_mcp                # listens on PLINTH_ASANA_MCP_PORT (7433)
```

To run the test suite:

```bash
pytest -q
```

## Configuration

Env vars (prefix `PLINTH_ASANA_MCP_`, all optional):

| Var                                       | Default                          | Purpose                       |
| ----------------------------------------- | -------------------------------- | ----------------------------- |
| `PLINTH_ASANA_MCP_PORT`                   | `7433`                           | TCP port to bind to           |
| `PLINTH_ASANA_MCP_API_BASE_URL`           | `https://app.asana.com/api/1.0`  | API root                      |
| `PLINTH_ASANA_MCP_REQUEST_TIMEOUT_SECONDS`| `15.0`                           | Per-request timeout           |
| `PLINTH_ASANA_MCP_LOG_LEVEL`              | `INFO`                           | Logging level                 |
| `PLINTH_ASANA_MCP_LOG_FORMAT`             | `console`                        | `console` or `json`           |
