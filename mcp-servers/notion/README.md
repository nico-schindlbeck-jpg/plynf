# Plinth Notion MCP Server

A minimal MCP-style server that wraps the Notion API for use with Plinth
agents. Runs at `http://localhost:7429` by default.

## What it does

Exposes seven tools that mirror the Notion REST surface most agents need:

| `tool_id`                  | Side effects | Notes                                           |
| -------------------------- | ------------ | ----------------------------------------------- |
| `notion.search`            | read         | Search across workspace pages and databases     |
| `notion.get_page`          | read         | Fetch a single page (properties + content)      |
| `notion.create_page`       | write        | Create a page (database child or page child)    |
| `notion.update_page`       | write        | Update properties or archive flag               |
| `notion.append_block`      | write        | Append blocks to an existing page               |
| `notion.list_databases`    | read         | List accessible databases                       |
| `notion.query_database`    | read         | Query a database with filter/sort               |

Every tool advertises `auth_method=oauth2` with `auth_config.provider="notion"`
in its metadata, so the Plinth gateway knows to inject the user's Notion
token on each call.

## Auth

This server **never holds the Notion access token**. The gateway forwards the
user's bearer token via the `Authorization` header on each `POST /invoke/...`
call; the tools then forward the same token to Notion. If the header is
missing or unparseable, the server returns 401 with a Plinth error envelope.

## Endpoints

```
GET  /healthz              -> {"status":"ok","version":"1.1.0","service":"notion-mcp"}
GET  /tools                -> list of 7 tools with input/output schemas
POST /invoke/{tool_name}   body: args  -> {"result": ...} | {"error": {...}}
GET  /metrics              -> Prometheus exposition
```

## Run locally

```bash
cd mcp-servers/notion
pip install -e ".[dev]"
python -m notion_mcp                # listens on PLINTH_NOTION_MCP_PORT (7429)
```

To run the test suite:

```bash
pytest -q
```

## Configuration

Env vars (prefix `PLINTH_NOTION_MCP_`, all optional):

| Var                                     | Default                      | Purpose                       |
| --------------------------------------- | ---------------------------- | ----------------------------- |
| `PLINTH_NOTION_MCP_PORT`                | `7429`                       | TCP port to bind to           |
| `PLINTH_NOTION_MCP_API_BASE_URL`        | `https://api.notion.com`     | Override for tests            |
| `PLINTH_NOTION_MCP_REQUEST_TIMEOUT_SECONDS` | `15.0`                   | Per-request timeout           |
| `PLINTH_NOTION_MCP_LOG_LEVEL`           | `INFO`                       | Standard log level            |
| `PLINTH_NOTION_MCP_LOG_FORMAT`          | `console`                    | `console` or `json`           |
| `PLINTH_NOTION_MCP_API_VERSION`         | `2022-06-28`                 | `Notion-Version` header       |

## Wiring with the gateway

After starting the server and the gateway, register each tool with the gateway
once. The handy way is to `GET http://localhost:7429/tools` and POST each
entry through `/v1/tools/register`, supplying the gateway-side endpoint and the
matching `connection_id` your agent obtained from the OAuth flow:

```bash
curl -s http://localhost:7429/tools | jq -c '.tools[]' | while read tool; do
  tool_id=$(echo "$tool" | jq -r '.tool_id')
  curl -s -X POST http://localhost:7422/v1/tools/register \
    -H "Authorization: Bearer local-dev" \
    -H "Content-Type: application/json" \
    -d "$(echo "$tool" | jq --arg ep "http://localhost:7429/invoke/$tool_id" \
        --arg cid "$CONNECTION_ID" \
        '. + {transport: "http", endpoint: $ep,
              auth_config: {provider: "notion", connection_id: $cid}}')"
done
```
