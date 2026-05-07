# Plinth Slack MCP Server

A minimal MCP-style server that wraps the Slack Web API for use with Plinth
agents. Runs at `http://localhost:7427` by default.

## What it does

Exposes four tools that mirror the Slack Web API surface most agents need:

| `tool_id`              | Side effects | Notes                                              |
| ---------------------- | ------------ | -------------------------------------------------- |
| `slack.list_channels`  | read         | Public + private channels visible to the token    |
| `slack.list_messages`  | read         | Recent messages from a channel (`conversations.history`) |
| `slack.post_message`   | write        | Post a message (or threaded reply) to a channel    |
| `slack.get_user`       | read         | Profile info for one user (`users.info`)           |

Every tool advertises `auth_method=oauth2` with `auth_config.provider="slack"`
in its metadata, so the Plinth gateway knows to inject the user's Slack token
on each call.

## Auth

This server **never holds the Slack access token**. The gateway forwards the
user's bearer token via the `Authorization` header on each `POST /invoke/...`
call; the tools then forward the same token to Slack. If the header is
missing or unparseable, the server returns 401 with a Plinth error envelope.

Slack's Web API returns `HTTP 200` even on application-level errors (with an
`{"ok": false, "error": "..."}` body). The server detects this and translates
known auth errors (`invalid_auth`, `not_authed`, `token_revoked`,
`token_expired`) into HTTP 401 / `UNAUTHORIZED`, and other errors into HTTP
400 / `TOOL_INVOCATION_FAILED` with the Slack error code in `details.slack_error`.

## Endpoints

```
GET  /healthz              -> {"status":"ok","version":"0.4.0","service":"slack-mcp"}
GET  /tools                -> list of 4 tools with input/output schemas
POST /invoke/{tool_name}   body: args  -> {"result": ...} | {"error": {...}}
```

## Run locally

```bash
cd mcp-servers/slack
pip install -e ".[dev]"
python -m slack_mcp                  # listens on PLINTH_SLACK_MCP_PORT (7427)
```

To run the test suite:

```bash
pytest -q
```

## Configuration

Env vars (prefix `PLINTH_SLACK_MCP_`, all optional):

| Var                                       | Default                  | Purpose                            |
| ----------------------------------------- | ------------------------ | ---------------------------------- |
| `PLINTH_SLACK_MCP_PORT`                   | `7427`                   | TCP port to bind to                |
| `PLINTH_SLACK_MCP_API_BASE_URL`           | `https://slack.com/api`  | Override for tests / mocks         |
| `PLINTH_SLACK_MCP_REQUEST_TIMEOUT_SECONDS`| `15.0`                   | Per-request timeout                |
| `PLINTH_SLACK_MCP_LOG_LEVEL`              | `INFO`                   | Standard log level                 |
| `PLINTH_SLACK_MCP_LOG_FORMAT`             | `console`                | `console` or `json`                |

## Setting up a Slack OAuth app

1. Go to <https://api.slack.com/apps> and click **Create New App** → **From scratch**.
2. Pick a name + workspace.
3. Under **OAuth & Permissions**:
   * Add the **Redirect URL** `http://localhost:7422/v1/oauth/slack/callback`
     (or whatever your Plinth gateway's `PLINTH_OAUTH_SLACK_REDIRECT_URI` is set to).
   * Under **Bot Token Scopes**, add at minimum:
     * `channels:read`
     * `chat:write`
     * `users:read`
     * (optional, for private channels: `groups:read`, `groups:history`)
4. Install the app to your workspace; copy the **Client ID** and **Client Secret**.
5. Export them when starting the gateway:

   ```bash
   export PLINTH_OAUTH_SLACK_CLIENT_ID=...
   export PLINTH_OAUTH_SLACK_CLIENT_SECRET=...
   export PLINTH_OAUTH_SLACK_SCOPES="channels:read,chat:write,users:read"
   ```

Slack's OAuth v2 flow does **not** support PKCE; the gateway's provider
registry already accounts for this (`pkce=False` for the `slack` provider).

## Wiring with the gateway

After starting the slack-mcp server and the gateway, register each tool with
the gateway once. Pull the metadata from `/tools` and POST each entry through
`/v1/tools/register`, supplying the gateway-side endpoint and the matching
`connection_id` your agent obtained from the Slack OAuth flow:

```bash
curl -s http://localhost:7427/tools | jq -c '.tools[]' | while read tool; do
  tool_id=$(echo "$tool" | jq -r '.tool_id')
  curl -s -X POST http://localhost:7422/v1/tools/register \
    -H "Authorization: Bearer local-dev" \
    -H "Content-Type: application/json" \
    -d "$(echo "$tool" | jq --arg ep "http://localhost:7427/invoke/$tool_id" \
        --arg cid "$CONNECTION_ID" \
        '. + {transport: "http", endpoint: $ep,
              auth_config: {provider: "slack", connection_id: $cid}}')"
done
```
