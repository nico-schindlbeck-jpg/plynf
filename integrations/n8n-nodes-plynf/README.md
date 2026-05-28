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

## License

Apache-2.0
