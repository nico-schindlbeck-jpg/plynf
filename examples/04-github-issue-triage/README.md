# Plynf example: GitHub issue triage

> First "real-world tool integration" example for Plynf: an agent that
> classifies issues on a GitHub repo (bug / feature / question / spam),
> writes a Markdown triage report, and optionally posts a summary comment
> on each issue.

This example uses the v0.3 OAuth flow:

* Tokens are minted via `GET /v1/oauth/github/authorize` on the gateway.
* They are encrypted at rest in the gateway's SQLite DB.
* The gateway attaches `Authorization: Bearer <token>` to every call to the
  `github-mcp` server, so the agent itself **never sees the token**.

There are two run modes:

| Mode             | Network calls? | Needs GitHub OAuth?       | Notes                                |
| ---------------- | -------------- | ------------------------- | ------------------------------------ |
| `--mode simulation` | none         | no                        | Uses 10 canned fixtures (default).   |
| `--mode live`       | yes          | yes (set up below)        | Real GitHub via gateway → github-mcp.|

## Quickstart — simulation mode

```bash
cd examples/04-github-issue-triage
pip install -e .
python triage_agent.py --repo demo/repo --limit 5 --mode simulation
```

You should see something like:

```
Triage agent — repo: 'demo/repo' (mode: simulation)
  Issues triaged     : 5
  bug               : 1
  feature           : 1
  question          : 2
  spam              : 1
  Report written     : .../reports/triage-demo-repo-simulation.md
```

The report itself is plain Markdown.

---

## Live mode — full setup

### 1. Create a GitHub OAuth app

1. Go to <https://github.com/settings/developers> → **OAuth Apps** →
   **New OAuth App**.
2. Fill in:
   * Application name: anything (e.g. `Plynf dev`).
   * Homepage URL: `http://localhost:7422`.
   * Authorization callback URL:
     `http://localhost:7422/v1/oauth/github/callback`.
3. After creating the app, note the **Client ID**, then click **Generate a new
   client secret** and copy that too.

> A **personal OAuth app** is sufficient for local development; you do not
> need a GitHub App / installation for this example.

### 2. Export credentials

```bash
export PLINTH_OAUTH_GITHUB_CLIENT_ID=<your-client-id>
export PLINTH_OAUTH_GITHUB_CLIENT_SECRET=<your-client-secret>
# Required for at-rest token encryption. Generate with:
#   python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
export PLINTH_OAUTH_ENCRYPTION_KEY=<base64-32-bytes>
# The gateway uses bearer auth for inbound calls; any non-empty token works.
export PLINTH_API_KEY=local-dev
```

If you skip `PLINTH_OAUTH_ENCRYPTION_KEY` in dev, the gateway will auto-create
one in `$PLINTH_DATA_DIR/gateway-oauth-key` and warn you. Production must
always provide it explicitly.

### 3. Start the services

In three separate terminals:

```bash
# (a) Gateway — port 7422
cd services/gateway
python -m plinth_gateway

# (b) GitHub MCP — port 7426
cd mcp-servers/github
python -m github_mcp

# (c) Workspace — port 7421 (optional; not required for this example)
cd services/workspace
python -m plinth_workspace
```

### 4. Run the OAuth flow once

Open this URL in a browser:

```
http://localhost:7422/v1/oauth/github/authorize?redirect_uri=http://localhost:7422/healthz&scopes=repo,read:user
```

Log in to GitHub, approve the scopes, and you'll be redirected to the
gateway's `/healthz` with a `?connection_id=conn_<ulid>` query parameter.
Copy that connection id — you'll need it next.

> **Tip**: in production you'd redirect to your own UI which receives the
> `connection_id`. For the example we redirect back to the gateway's healthz
> just to read the connection id from the URL bar.

### 5. Register the github-mcp tools with the gateway

```bash
export PLINTH_GITHUB_CONNECTION_ID=conn_<ulid-from-step-4>
export PLINTH_API_KEY=local-dev

curl -s http://localhost:7426/tools | jq -c '.tools[]' | while read tool; do
  tool_id=$(echo "$tool" | jq -r '.tool_id')
  curl -s -X POST http://localhost:7422/v1/tools/register \
    -H "Authorization: Bearer ${PLINTH_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$(echo "$tool" | jq --arg ep "http://localhost:7426/invoke/$tool_id" \
            --arg cid "$PLINTH_GITHUB_CONNECTION_ID" \
            '. + {transport: "http", endpoint: $ep,
                  auth_config: {provider: "github", connection_id: $cid}}')"
done
```

(Repeat-safe — the gateway returns 400 for already-registered tools.)

### 6. Run the agent

```bash
python triage_agent.py --repo myorg/myrepo --limit 10 --mode live
```

To also post a summary comment on each issue (off by default):

```bash
python triage_agent.py --repo myorg/myrepo --mode live --post-comments
```

The agent reads:

* `PLINTH_GATEWAY_URL` (default `http://localhost:7422`)
* `PLINTH_GITHUB_MCP_URL` (default `http://localhost:7426`)
* `PLINTH_API_KEY` (default `local-dev`)

---

## What the agent actually does

```text
list_issues  ──┐
               │  for each:
get_issue   ──┤    classify_issue(title, body) → category + confidence
               │
write report ──┴──> ./reports/triage-<repo>-<mode>.md
```

In simulation mode, the classifier runs over the fixtures in `shared.py`. In
live mode it runs over real GitHub data, but the *classifier itself is
identical* — keeping the demo deterministic and offline-friendly while still
exercising the full OAuth path on `--mode=live`.

## Where to look in the code

* `triage_agent.py` — the agent + CLI.
* `shared.py` — mock LLM, fixtures, gateway client.
* `reports/` — Markdown output goes here.
