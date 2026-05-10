# Plinth Atlassian MCP Server

A minimal MCP-style server that wraps the Atlassian (Jira + Confluence) REST
APIs for use with Plinth agents. Runs at `http://localhost:7431` by default.

## What it does

Exposes eight tools that mirror the slice of Jira + Confluence most agents
need:

| `tool_id`                              | Side effects | Notes                                           |
| -------------------------------------- | ------------ | ----------------------------------------------- |
| `atlassian.jira_search`                | read         | JQL search across the workspace                 |
| `atlassian.jira_get_issue`             | read         | Single issue with comments                      |
| `atlassian.jira_create_issue`          | write        | Project key + summary; description in ADF       |
| `atlassian.jira_update_issue`          | write        | PUT fields (summary, status, custom fields)     |
| `atlassian.jira_comment`               | write        | Add a comment to a Jira issue                   |
| `atlassian.confluence_search`          | read         | CQL search across pages                         |
| `atlassian.confluence_get_page`        | read         | Page with storage-format body                   |
| `atlassian.confluence_create_page`     | write        | Create a page in a space                        |

Every tool advertises `auth_method=oauth2` with `auth_config.provider="atlassian"`
in its metadata, so the Plinth gateway knows to inject the user's Atlassian
token + cloudid on each call.

## Auth + cloudid

Atlassian's 3LO OAuth flow doesn't bind a token to a single workspace. A
single OAuth grant can span multiple "cloud" workspaces — every REST call
needs to specify which workspace via the `cloudid` (a UUID returned by
`https://api.atlassian.com/oauth/token/accessible-resources`).

The Plinth gateway captures the cloudid at OAuth callback time and stores
it in `connection.metadata`. On every proxied invoke it forwards both:

* `Authorization: Bearer <access_token>`
* `X-Plinth-OAuth-Cloudid: <cloudid>`

This MCP server reads both headers and forms per-call URLs like
`https://api.atlassian.com/ex/jira/{cloudid}/rest/api/3/...`. Missing
`Authorization` returns 401; missing `X-Plinth-OAuth-Cloudid` returns 400
with `code=ATLASSIAN_CLOUDID_MISSING`.

## Endpoints

```
GET  /healthz              -> {"status":"ok","version":"1.5.0","service":"atlassian-mcp"}
GET  /tools                -> list of 8 tools with input/output schemas
POST /invoke/{tool_name}   body: args  -> {"result": ...} | {"error": {...}}
GET  /metrics              -> Prometheus exposition
```

## Run locally

```bash
cd mcp-servers/atlassian
pip install -e ".[dev]"
python -m atlassian_mcp                # listens on PLINTH_ATLASSIAN_MCP_PORT (7431)
```

To run the test suite:

```bash
pytest -q
```

## Configuration

Env vars (prefix `PLINTH_ATLASSIAN_MCP_`, all optional):

| Var                                          | Default                       | Purpose                       |
| -------------------------------------------- | ----------------------------- | ----------------------------- |
| `PLINTH_ATLASSIAN_MCP_PORT`                  | `7431`                        | TCP port to bind to           |
| `PLINTH_ATLASSIAN_MCP_API_BASE_URL`          | `https://api.atlassian.com`   | API root                      |
| `PLINTH_ATLASSIAN_MCP_REQUEST_TIMEOUT_SECONDS` | `15.0`                      | Per-request timeout           |
| `PLINTH_ATLASSIAN_MCP_LOG_LEVEL`             | `INFO`                        | Logging level                 |
| `PLINTH_ATLASSIAN_MCP_LOG_FORMAT`            | `console`                     | `console` or `json`           |
