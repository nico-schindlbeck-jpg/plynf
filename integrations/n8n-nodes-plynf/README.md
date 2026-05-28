# n8n-nodes-plynf

n8n community node for [Plynf](https://plynf.com) — the agent context
optimization layer.

Drop the **Plynf** node into any n8n workflow that calls Salesforce, Slack,
your order DB, or any registered Plynf connector. The node fetches the
data, applies your Plynf shaping policy, and returns a *small* JSON
payload your LLM step can use directly.

## Install

In n8n:

1. Settings → Community Nodes → Install
2. Enter `n8n-nodes-plynf`
3. Save

## Configure credentials

Add a new **Plynf API** credential:

| Field | Value |
|---|---|
| Plynf URL | `https://app.plynf.com` (or your self-hosted proxy URL) |
| API Key | issued at [app.plynf.com](https://app.plynf.com) — free tier available |

## Use

1. Add the **Plynf** node to a workflow
2. Pick a tool (e.g. *Order DB · Get Order*)
3. Provide arguments as JSON (e.g. `{"order_id": "12345"}`)
4. Pipe the output's `result` field into your LLM node

The node also returns `cache_hit` and `savings` so you can show the
real-time token reduction in the workflow.

## Build

```bash
npm install
npm run build
```

## Publish

```bash
# One-time
npm login

# Publish to npm (already-reserved name n8n-nodes-plynf)
npm publish --access public
```

Once published, anyone can install it via the n8n UI:
*Settings → Community Nodes → Install → `n8n-nodes-plynf`*. No n8n team
approval is needed for npm-installable community nodes. For the
verified-list halo, PR your repo at
[n8n-io/n8n-nodes-community](https://github.com/n8n-io/n8n-nodes-community).

## Workflow templates

`templates/customer-support-agent.json` — Slack-mention → Plynf-shape →
gpt-4o reply → post back. Import via n8n's
*Workflows → Import from File*. Replace
`REPLACE_WITH_YOUR_PLYNF_CREDENTIAL_ID` with the credential id you create
in the n8n credential manager.

## Tier behaviour (what happens when you hit Free-tier limits)

The proxy enforces tier gating; the node passes through whatever it gets
back. Free tier caps at 100 000 shaped tokens / month + 3 connectors;
when exceeded the proxy returns HTTP 402 and the n8n step fails with the
proxy's `upgrade_hint` text. Toggle *Continue On Fail* on the Plynf node
if you want the workflow to keep going (e.g. fall back to raw data).

## License

Apache-2.0
