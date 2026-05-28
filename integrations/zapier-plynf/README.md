# zapier-plynf

Plynf — Agent Context Optimization Layer for Zapier.

Plug Plynf into any Zap that calls Salesforce / Slack / Order-DB and your
LLM step (OpenAI, Anthropic, …) sees 40–80% fewer tokens per tool
response. One action covers all Plynf-managed tools.

## Develop

```bash
npm install
npm install -g zapier-platform-cli
zapier validate
zapier test
```

## Publish

```bash
zapier register "Plynf"
zapier push
zapier promote 0.1.0
```

After Zapier review approves the app, users find it in the Zapier app
directory by searching for "Plynf".

## Auth

Users connect once with their Plynf URL + API key. The credential test
calls `GET /v1/tier` so we surface the tenant id + tier in the
connection label (e.g. `tenant-acme (pro)`).

## Tier behaviour

Plynf's tier gate runs on the proxy side. If a Free-tier tenant exceeds
their monthly shaped-token budget, the proxy returns HTTP 402 with an
upgrade hint — Zapier surfaces it as a clear error in the Zap history.
No special handling needed in the Zapier app itself.

## Action: "Fetch & shape a tool response"

Inputs:
- **Tool** (dropdown of Plynf-managed tools)
- **Arguments (JSON)** — passed straight to the tool
- **Agent ID** — optional; shows up in your Plynf savings dashboard

Outputs include the shaped `result` and a `savings` block (`saved_tokens`,
`savings_pct`) you can pipe into a Slack notification or a dashboard step
for visible ROI.
