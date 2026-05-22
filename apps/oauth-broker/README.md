# Plynf OAuth Broker

A tiny Cloudflare Workers app that brokers OAuth flows between local Plynf installs and three providers (GitHub, Linear, Notion) so end users don't have to register their own OAuth applications.

## Why this exists

Without a broker, every Plynf user who wants to connect their GitHub account has to:

1. Go to github.com/settings/developers
2. Register a new OAuth App
3. Configure callback URL, scopes
4. Copy the client_id and client_secret
5. Set two environment variables in their `.env`
6. Restart the gateway

Repeat for Slack, Linear, Notion, Google, etc. That's hours of paperwork before the agent runs.

The broker does it once per provider, on Plynf's side. The local Plynf install just says "go connect to GitHub" and the broker handles the rest. Users see the GitHub consent screen (showing "Plynf" as the requesting app, not their own random OAuth app), click Allow, and they're done.

## Architecture in one paragraph

The broker is **stateless except for short-lived CSRF/PKCE state in Cloudflare KV** (15 min TTL). When a local Plynf install POSTs to `/v1/oauth/start`, the broker generates a PKCE verifier + state token, stores them in KV under the state, and returns the provider's authorize URL. The user consents, the provider redirects to `oauth.plynf.com/v1/oauth/cb?code=...&state=...`, the broker looks up the state, exchanges the code for a token, and **redirects to the local Plynf install with the token in the URL fragment** (not the query — fragments aren't sent in `Referer` headers or proxy logs). The token transits the broker for milliseconds. It is **never written to KV, never logged.** A test asserts this.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/health`            | Uptime probe. Returns `{"ok": true}`. |
| `POST` | `/v1/oauth/start`    | Begin a flow. Body: `{provider, local_callback, scopes?, state?}`. Returns `{authorize_url, state}`. |
| `GET`  | `/v1/oauth/cb`       | Provider redirect target. Exchanges code, redirects to `local_callback#access_token=...`. |

## Local development

```bash
cd apps/oauth-broker
npm install
npm run dev          # wrangler dev — local Workers runtime on :8787
```

To test against mock OAuth providers without real credentials:

```bash
npm test             # vitest, hermetic, no network
```

## Deployment

The broker runs on Cloudflare Workers (free tier handles >100k req/day easily — our expected traffic is in the hundreds per day even at scale).

```bash
# One-time setup
npx wrangler kv namespace create STATE
# → copy the id into wrangler.toml's [[kv_namespaces]] section

# Set production secrets (Notion only — GitHub and Linear are PKCE-only)
npx wrangler secret put NOTION_CLIENT_SECRET

# Deploy
npm run deploy
```

The `oauth.plynf.com` subdomain needs to be set up in Cloudflare DNS first (CNAME flattening or just `routes` in wrangler.toml — the latter is configured).

## Threat model summary

What an attacker who compromises the broker can do:

- **Replay codes**: every code-state pair is single-use (deleted on exchange). Replay is impossible.
- **Steal tokens**: would require either compromising the Workers runtime (Cloudflare's job) or intercepting the redirect to the local loopback (requires local-machine access — they've already won).
- **Phishing**: a malicious `local_callback` is rejected unless it's a 127.0.0.1 / localhost URL. SSRF impossible.

What it can NOT do:

- Issue tokens for arbitrary providers — the provider validates the OAuth app via the registered client_id, so the broker can only mint tokens for the apps Plynf has registered.
- Retain tokens — KV is the only persistence and the access-log + test suite verify it never sees a token-shaped string.

## Provider registration checklist

These three OAuth apps must be created before deploy:

| Provider | Where to register | Callback URL |
|---|---|---|
| GitHub | github.com/settings/developers → New OAuth App | `https://oauth.plynf.com/v1/oauth/cb` |
| Linear | linear.app/settings/api/applications → Create | `https://oauth.plynf.com/v1/oauth/cb` |
| Notion | notion.so/profile/integrations → New integration | `https://oauth.plynf.com/v1/oauth/cb` |

Slack, Google Workspace, Atlassian, Salesforce, Asana are NOT in this broker — they require months-long review processes for distribution-grade OAuth apps. Track those in Block 7b/c of the roadmap.

## Status

🟡 **Scaffolded, not yet deployed.** Block 7a from the open-tasks list. Local tests pass; first real deploy waits for the GitHub Org name (B3) and Cloudflare Workers account setup. Once the three OAuth apps are registered, `npm run deploy` brings it live in <30 seconds.
