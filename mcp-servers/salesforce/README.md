# Plinth Salesforce MCP Server

A minimal MCP-style server that wraps the Salesforce REST API for use with
Plinth agents. Runs at `http://localhost:7432` by default.

## What it does

Exposes six tools that cover the Salesforce REST surface most agents need:

| `tool_id`                       | Side effects | Notes                                   |
| ------------------------------- | ------------ | --------------------------------------- |
| `salesforce.soql_query`         | read         | Run a SOQL query                        |
| `salesforce.get_record`         | read         | Get a single record                     |
| `salesforce.create_record`      | write        | Create Lead/Contact/Opportunity/etc.    |
| `salesforce.update_record`      | write        | PATCH fields onto a record              |
| `salesforce.delete_record`      | write        | Delete a record                         |
| `salesforce.list_objects`       | read         | List org's SObject types + their schema |

Every tool advertises `auth_method=oauth2` with `auth_config.provider="salesforce"`.

## Auth + instance_url

Salesforce's OAuth flow returns an `instance_url` per token (the per-org REST
API base, e.g. `https://acme.my.salesforce.com`). The Plinth gateway captures
this from the token-exchange response, persists it in
`connection.metadata.instance_url`, and forwards it on every proxied invoke as:

* `Authorization: Bearer <access_token>`
* `X-Plinth-OAuth-InstanceUrl: <instance_url>`

This MCP server reads both headers and forms per-call URLs like
`{instance_url}/services/data/{api_version}/...`. The instance_url is
validated as an HTTPS URL pointing at a known Salesforce domain
(`*.salesforce.com`, `*.force.com`, etc.) before being used. Missing
`Authorization` returns 401; missing `X-Plinth-OAuth-InstanceUrl` returns
400 with `code=SALESFORCE_INSTANCE_URL_MISSING`.

## Endpoints

```
GET  /healthz              -> {"status":"ok","version":"1.5.0","service":"salesforce-mcp"}
GET  /tools                -> list of 6 tools with input/output schemas
POST /invoke/{tool_name}   body: args  -> {"result": ...} | {"error": {...}}
GET  /metrics              -> Prometheus exposition
```

## Run locally

```bash
cd mcp-servers/salesforce
pip install -e ".[dev]"
python -m salesforce_mcp                # listens on PLINTH_SALESFORCE_MCP_PORT (7432)
```

To run the test suite:

```bash
pytest -q
```

## Configuration

Env vars (prefix `PLINTH_SALESFORCE_MCP_`, all optional):

| Var                                            | Default     | Purpose                       |
| ---------------------------------------------- | ----------- | ----------------------------- |
| `PLINTH_SALESFORCE_MCP_PORT`                   | `7432`      | TCP port to bind to           |
| `PLINTH_SALESFORCE_MCP_API_VERSION`            | `v60.0`     | Salesforce REST API version   |
| `PLINTH_SALESFORCE_MCP_REQUEST_TIMEOUT_SECONDS`| `15.0`      | Per-request timeout           |
| `PLINTH_SALESFORCE_MCP_LOG_LEVEL`              | `INFO`      | Logging level                 |
| `PLINTH_SALESFORCE_MCP_LOG_FORMAT`             | `console`   | `console` or `json`           |
