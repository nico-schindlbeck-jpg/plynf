# Plynf Linear MCP Server

A minimal MCP-style server that wraps Linear's GraphQL API for use with Plynf
agents. Runs at `http://localhost:7428` by default.

## What it does

Exposes five tools that mirror the Linear GraphQL surface most agents need:

| `tool_id`                  | Side effects | Notes                                                 |
| -------------------------- | ------------ | ----------------------------------------------------- |
| `linear.list_issues`       | read         | List issues, filterable by team / assignee / state    |
| `linear.get_issue`         | read         | Fetch a single issue + its comments                   |
| `linear.create_issue`      | write        | Open a new issue in a team                            |
| `linear.update_issue`      | write        | Edit title / description / state / assignee / labels  |
| `linear.comment_on_issue`  | write        | Post a comment on an issue                            |

Every tool advertises `auth_method=oauth2` with `auth_config.provider="linear"`
in its metadata, so the Plynf gateway knows to inject the user's Linear token
on each call.

## Auth

This server **never holds the Linear access token**. The gateway forwards the
user's bearer token via the `Authorization` header on each `POST /invoke/...`
call; the tools then forward the same token to Linear's GraphQL endpoint. If
the header is missing or unparseable, the server returns 401 with a Plynf
error envelope.

GraphQL errors (`errors` array in the response body) are translated into a
Plynf error envelope. Authentication-class errors (`extensions.code` of
`AUTHENTICATION_ERROR`/`UNAUTHENTICATED`, or messages mentioning
"authentication") map to HTTP 401 / `UNAUTHORIZED`.

## Endpoints

```
GET  /healthz              -> {"status":"ok","version":"0.4.0","service":"linear-mcp"}
GET  /tools                -> list of 5 tools with input/output schemas
POST /invoke/{tool_name}   body: args  -> {"result": ...} | {"error": {...}}
```

## Run locally

```bash
cd mcp-servers/linear
pip install -e ".[dev]"
python -m linear_mcp                  # listens on PLINTH_LINEAR_MCP_PORT (7428)
```

To run the test suite:

```bash
pytest -q
```

## Configuration

Env vars (prefix `PLINTH_LINEAR_MCP_`, all optional):

| Var                                        | Default                            | Purpose                            |
| ------------------------------------------ | ---------------------------------- | ---------------------------------- |
| `PLINTH_LINEAR_MCP_PORT`                   | `7428`                             | TCP port to bind to                |
| `PLINTH_LINEAR_MCP_GRAPHQL_URL`            | `https://api.linear.app/graphql`   | GraphQL endpoint                   |
| `PLINTH_LINEAR_MCP_REQUEST_TIMEOUT_SECONDS`| `15.0`                             | Per-request timeout                |
| `PLINTH_LINEAR_MCP_LOG_LEVEL`              | `INFO`                             | Standard log level                 |
| `PLINTH_LINEAR_MCP_LOG_FORMAT`             | `console`                          | `console` or `json`                |

## Setting up a Linear OAuth app

1. Go to <https://linear.app/settings/api/applications>.
2. Click **Create new application**.
3. Set the **Redirect URLs** to
   `http://localhost:7422/v1/oauth/linear/callback` (or whatever your Plynf
   gateway's `PLINTH_OAUTH_LINEAR_REDIRECT_URI` is set to).
4. Request the scopes you need — at minimum `read` and `write`.
5. Copy the **Client ID** and **Client secret**.
6. Export them when starting the gateway:

   ```bash
   export PLINTH_OAUTH_LINEAR_CLIENT_ID=...
   export PLINTH_OAUTH_LINEAR_CLIENT_SECRET=...
   export PLINTH_OAUTH_LINEAR_SCOPES="read,write"
   ```

Linear's OAuth flow uses standard OAuth 2.0 with PKCE; the gateway already
generates the verifier/challenge automatically.

## Wiring with the gateway

After starting the linear-mcp server and the gateway, register each tool with
the gateway once. Pull the metadata from `/tools` and POST each entry through
`/v1/tools/register`, supplying the gateway-side endpoint and the matching
`connection_id` your agent obtained from the Linear OAuth flow:

```bash
curl -s http://localhost:7428/tools | jq -c '.tools[]' | while read tool; do
  tool_id=$(echo "$tool" | jq -r '.tool_id')
  curl -s -X POST http://localhost:7422/v1/tools/register \
    -H "Authorization: Bearer local-dev" \
    -H "Content-Type: application/json" \
    -d "$(echo "$tool" | jq --arg ep "http://localhost:7428/invoke/$tool_id" \
        --arg cid "$CONNECTION_ID" \
        '. + {transport: "http", endpoint: $ep,
              auth_config: {provider: "linear", connection_id: $cid}}')"
done
```
