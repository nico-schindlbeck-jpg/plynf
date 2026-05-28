# Plynf · Slack app

Bring Plynf to Slack AI assistants and to interactive Slack workflows.

The Slack manifest in `manifest.yaml` registers:

- A bot user `@Plynf` users can DM or mention.
- A slash command `/plynf-fetch get_order {"order_id":"12345"}`.
- An event subscription pointed at `app.plynf.com/integrations/slack/*`
  so the proxy receives `app_mention`, `message.im`, and the new
  `assistant_thread_*` events that Slack AI Agents emit.

## Install the app

1. Create at https://api.slack.com/apps → *Create New App* → *From a manifest*.
2. Paste `manifest.yaml` and pick the workspace.
3. After install, copy the Bot User OAuth Token + Signing Secret into
   your Plynf proxy configuration (env vars
   `PLINTH_PROXY_SLACK_BOT_TOKEN` and `PLINTH_PROXY_SLACK_SIGNING_SECRET`
   — server-side handler comes in the next sprint).

## Three usage modes

1. **Slash command in any channel** — `/plynf-fetch get_lead {"id":"00Q..."}`
   returns the shaped Lead in an ephemeral message.
2. **DM the bot** — sending the bot `get_order #12345` triggers the same
   shaping flow. Useful for an AI-assistant demo without changing the
   workflow.
3. **Slack AI Agents** — the `assistant_thread_*` events let Plynf
   participate in Slack AI's official thread context, so Plynf-shaped
   data is one of the sources the in-Slack AI can reference.

## Server-side handler

This skeleton ships only the manifest. The proxy-side handler endpoints
(`/integrations/slack/events`, `/integrations/slack/slash`,
`/integrations/slack/interactivity`) live with the other webhook routes
under `services/proxy/src/plinth_proxy/api.py` and verify Slack's
signing secret before invoking the tool pipeline.

The matching backend lands in a follow-up commit once we have a Slack
sandbox to validate against — Slack's signing-secret verification is
tedious to mock and isn't worth doing without a real workspace to
round-trip against.

## Tier behaviour

Same as every other integration: the proxy enforces the tier gate.
Free-tier teams that exceed 100k tokens/month get an HTTP 402 from the
proxy; the Slack handler will surface this as an ephemeral message
"You've hit your Plynf free tier — upgrade at app.plynf.com to keep
this assistant running" rather than a generic error.
