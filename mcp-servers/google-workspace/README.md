# Plinth Google Workspace MCP Server

A minimal MCP-style server that wraps Google Workspace APIs (Drive, Docs,
Sheets, Calendar, Gmail) for use with Plinth agents. Runs at
`http://localhost:7430` by default.

## What it does

Exposes eight tools that mirror the Google Workspace surface most agents need:

| `tool_id`                         | Side effects | Notes                                       |
| --------------------------------- | ------------ | ------------------------------------------- |
| `google.drive_search`             | read         | List files matching a query                 |
| `google.drive_read`               | read         | Read a file's content (export Doc/Sheet)    |
| `google.docs_create`              | write        | Create a new Doc with optional content      |
| `google.docs_append`              | write        | Append plain-text content to a Doc          |
| `google.sheets_read`              | read         | Read a range from a Sheet                   |
| `google.sheets_append_row`        | write        | Append a row to a Sheet                     |
| `google.calendar_list_events`     | read         | List upcoming events on a calendar          |
| `google.gmail_list_messages`      | read         | List inbox messages with header summary     |

Every tool advertises `auth_method=oauth2` with `auth_config.provider="google"`
in its metadata, so the Plinth gateway knows to inject the user's Google
token on each call.

## Auth

This server **never holds the Google access token**. The gateway forwards the
user's bearer token via the `Authorization` header on each `POST /invoke/...`
call; the tools then forward the same token to Google. If the header is
missing or unparseable, the server returns 401 with a Plinth error envelope.

## Endpoints

```
GET  /healthz              -> {"status":"ok","version":"1.1.0","service":"google-workspace-mcp"}
GET  /tools                -> list of 8 tools with input/output schemas
POST /invoke/{tool_name}   body: args  -> {"result": ...} | {"error": {...}}
GET  /metrics              -> Prometheus exposition
```

## Run locally

```bash
cd mcp-servers/google-workspace
pip install -e ".[dev]"
python -m google_workspace_mcp     # listens on PLINTH_GOOGLE_MCP_PORT (7430)
```

To run the test suite:

```bash
pytest -q
```

## Configuration

Env vars (prefix `PLINTH_GOOGLE_MCP_`, all optional):

| Var                                         | Default                         | Purpose                       |
| ------------------------------------------- | ------------------------------- | ----------------------------- |
| `PLINTH_GOOGLE_MCP_PORT`                    | `7430`                          | TCP port to bind to           |
| `PLINTH_GOOGLE_MCP_DRIVE_BASE_URL`          | `https://www.googleapis.com`    | Drive API root                |
| `PLINTH_GOOGLE_MCP_DOCS_BASE_URL`           | `https://docs.googleapis.com`   | Docs API root                 |
| `PLINTH_GOOGLE_MCP_SHEETS_BASE_URL`         | `https://sheets.googleapis.com` | Sheets API root               |
| `PLINTH_GOOGLE_MCP_GMAIL_BASE_URL`          | `https://gmail.googleapis.com`  | Gmail API root                |
| `PLINTH_GOOGLE_MCP_REQUEST_TIMEOUT_SECONDS` | `15.0`                          | Per-request timeout           |
| `PLINTH_GOOGLE_MCP_LOG_LEVEL`               | `INFO`                          | Standard log level            |
| `PLINTH_GOOGLE_MCP_LOG_FORMAT`              | `console`                       | `console` or `json`           |

## Privacy notes

`google.gmail_list_messages` returns only header metadata + snippet (subject,
from, date, snippet) — never the full body. Agents that need body content
should request it explicitly via a separate tool.

## Wiring with the gateway

After starting the server and the gateway, register each tool with the gateway
once:

```bash
curl -s http://localhost:7430/tools | jq -c '.tools[]' | while read tool; do
  tool_id=$(echo "$tool" | jq -r '.tool_id')
  curl -s -X POST http://localhost:7422/v1/tools/register \
    -H "Authorization: Bearer local-dev" \
    -H "Content-Type: application/json" \
    -d "$(echo "$tool" | jq --arg ep "http://localhost:7430/invoke/$tool_id" \
        --arg cid "$CONNECTION_ID" \
        '. + {transport: "http", endpoint: $ep,
              auth_config: {provider: "google", connection_id: $cid}}')"
done
```
