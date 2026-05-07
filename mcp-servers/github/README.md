# Plinth GitHub MCP Server

A minimal MCP-style server that wraps the GitHub REST API for use with Plinth
agents. Runs at `http://localhost:7426` by default.

## What it does

Exposes seven tools that mirror the GitHub REST surface most agents need:

| `tool_id`                  | Side effects | Notes                                        |
| -------------------------- | ------------ | -------------------------------------------- |
| `github.list_issues`       | read         | List issues for a repo (PRs filtered out)    |
| `github.get_issue`         | read         | Fetch a single issue + its comments          |
| `github.create_issue`      | write        | Open a new issue                             |
| `github.update_issue`      | write        | Edit title / body / state / labels           |
| `github.comment_on_issue`  | write        | Post a comment                               |
| `github.get_repo`          | read         | Repo metadata                                |
| `github.search_code`       | read         | Code search (optionally scoped to a repo)    |

Every tool advertises `auth_method=oauth2` with `auth_config.provider="github"`
in its metadata, so the Plinth gateway knows to inject the user's GitHub token
on each call.

## Auth

This server **never holds the GitHub access token**. The gateway forwards the
user's bearer token via the `Authorization` header on each `POST /invoke/...`
call; the tools then forward the same token to GitHub. If the header is
missing or unparseable, the server returns 401 with a Plinth error envelope.

## Endpoints

```
GET  /healthz              -> {"status":"ok","version":"0.3.0","service":"github-mcp"}
GET  /tools                -> list of 7 tools with input/output schemas
POST /invoke/{tool_name}   body: args  -> {"result": ...} | {"error": {...}}
```

## Run locally

```bash
cd mcp-servers/github
pip install -e ".[dev]"
python -m github_mcp                # listens on PLINTH_GITHUB_PORT (7426)
```

To run the test suite:

```bash
pytest -q
```

## Configuration

Env var (prefix `PLINTH_GITHUB_`, all optional):

| Var                               | Default                  | Purpose                            |
| --------------------------------- | ------------------------ | ---------------------------------- |
| `PLINTH_GITHUB_PORT`              | `7426`                   | TCP port to bind to                |
| `PLINTH_GITHUB_API_BASE_URL`      | `https://api.github.com` | Override for tests / GH Enterprise |
| `PLINTH_GITHUB_REQUEST_TIMEOUT_SECONDS` | `15.0`             | Per-request timeout                |
| `PLINTH_GITHUB_LOG_LEVEL`         | `INFO`                   | Standard log level                 |
| `PLINTH_GITHUB_LOG_FORMAT`        | `console`                | `console` or `json`                |
| `PLINTH_GITHUB_API_VERSION`       | `2022-11-28`             | `X-GitHub-Api-Version` header      |

`PLINTH_MOCK_PORT` is also honoured (overrides the port) for parity with the
existing example smoke-test commands.

## Wiring with the gateway

After starting the server and the gateway, register each tool with the gateway
once. The handy way is to `GET http://localhost:7426/tools` and POST each
entry through `/v1/tools/register`, supplying the gateway-side endpoint and the
matching `connection_id` your agent obtained from the OAuth flow:

```bash
curl -s http://localhost:7426/tools | jq -c '.tools[]' | while read tool; do
  tool_id=$(echo "$tool" | jq -r '.tool_id')
  curl -s -X POST http://localhost:7422/v1/tools/register \
    -H "Authorization: Bearer local-dev" \
    -H "Content-Type: application/json" \
    -d "$(echo "$tool" | jq --arg ep "http://localhost:7426/invoke/$tool_id" \
        --arg cid "$CONNECTION_ID" \
        '. + {transport: "http", endpoint: $ep,
              auth_config: {provider: "github", connection_id: $cid}}')"
done
```

(See `examples/04-github-issue-triage/` for a runnable end-to-end demo.)
