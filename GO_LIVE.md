# Go live — the exact steps

A precise, ordered runbook to take Plynf from "works on my laptop" to a public
product at **plynf.com**. Written for a non-developer: do the steps top to
bottom. Steps marked **[needs a dev]** are the two that genuinely want an
engineer for ~an afternoon (hosting + TLS). Everything else is accounts,
copy-paste, and clicks.

Each step ends with **Done when:** — don't move on until that's true.

---

## 0. The map (what serves what)

You'll end up with four public surfaces on your domain:

| URL | What it is | How it's served |
|-----|-----------|-----------------|
| `plynf.com` | Marketing site + the dashboard UI + `/install`, `/partners` | Netlify (static) |
| `app.plynf.com` | Dashboard **API** + the LLM **proxy** (`/v1`, `/v1/messages`, `/api/chat`) | Your host (step 4–5) |
| `oauth.plynf.com` | OAuth broker for one-click tool connectors | Cloudflare Worker (step 8) |
| `docs.plynf.com` | Docs (optional, later) | — |

Two backend groups run behind `app.plynf.com`:
- **Runtime services**: dashboard (7424), identity (7425), gateway (7422),
  workspace (7421) — shipped in `deploy/compose.prod.yml` + Helm.
- **The proxy** (the token-shaping LLM front door, `services/proxy`, port 7430)
  — deployed alongside; this is what SDKs point their base URL at.

---

## 1. Accounts to create (15 min)

- [ ] A domain you control (**plynf.com**) at any registrar.
- [ ] **Netlify** account (free) — hosts the site.
- [ ] **Cloudflare** account (free) — hosts the OAuth broker + manages DNS (easiest).
- [ ] A host for the backend (pick one): **Hetzner/DigitalOcean VPS** (cheapest, [needs a dev]) **or** **Render/Fly.io** (managed, deploy files already in `deploy/`).
- [ ] An **OpenAI** (or Anthropic/etc.) API key — the upstream the proxy forwards to.
- [ ] An **email** provider for `partners@` / `support@` (e.g. Cloudflare Email Routing — free).
- [ ] Later: **npm**, **Zapier developer**, **Make developer**, **Stripe** (see step 9–11).

---

## 2. DNS records (10 min, in Cloudflare)

Point the domain's nameservers at Cloudflare, then add:

```
A/CNAME   plynf.com          → Netlify (Netlify gives you the target)
CNAME     www.plynf.com      → plynf.com
A/CNAME   app.plynf.com      → your backend host's IP / hostname (step 4)
CNAME     oauth.plynf.com    → (Cloudflare Worker route, created in step 8)
```

**Done when:** `plynf.com` and `app.plynf.com` resolve (`ping plynf.com`).

---

## 3. Generate your secrets (5 min) — keep these private

Run locally:

```bash
# Identity JWT signing secret (used to sign dashboard session tokens)
openssl rand -base64 48

# A tenant API key for the proxy. Format is  tenant:key:tier
echo "acme:plynf_sk_live_$(openssl rand -hex 24):enterprise"
```

Save both in your host's secret manager (next step). **Never commit them.**

**Done when:** you have a JWT secret and at least one `tenant:key:tier` string.

---

## 4. Deploy the runtime services [needs a dev] (1–2 h)

**Option A — one VPS (recommended, cheapest).**
1. SSH to the VPS, install Docker.
2. Copy `deploy/compose.prod.yml` + a `.env` with at least:
   ```
   PLINTH_IDENTITY_JWT_SECRET=<the secret from step 3>
   PLINTH_LOG_FORMAT=json
   ```
3. Put **Caddy** in front for automatic HTTPS + path routing. Minimal `Caddyfile`:
   ```
   app.plynf.com {
     handle /api/app/*   { reverse_proxy localhost:7424 }   # dashboard API
     handle /v1/*        { reverse_proxy localhost:7430 }   # proxy (OpenAI door)
     handle /v1/messages { reverse_proxy localhost:7430 }   # Anthropic door
     handle /api/chat*   { reverse_proxy localhost:7430 }   # Ollama door
     handle              { reverse_proxy localhost:7424 }   # everything else → dashboard
   }
   ```
4. `docker compose -f deploy/compose.prod.yml up -d` and run Caddy.

**Option B — managed (Render/Fly).** Use the configs in `deploy/` to create one
service per component; each gets its own HTTPS URL. Then map `app.plynf.com`
to the dashboard + proxy (a small reverse-proxy or Fly `[[services]]` routing).

**Done when:** `curl https://app.plynf.com/healthz` returns `{"status":"ok"…}`.

---

## 5. Deploy the proxy + point it at a real model (30 min)

The proxy (`services/proxy`) runs as its own container/process on **7430**. Set:

```
PLINTH_PROXY_DEMO_MODE=false
PLINTH_PROXY_UPSTREAM_BASE_URL=https://api.openai.com
PLINTH_PROXY_UPSTREAM_API_KEY=<your OpenAI key>
PLINTH_PROXY_API_KEYS=<the tenant:key:tier from step 3>
PLINTH_PROXY_IDENTITY_URL=https://app.plynf.com        # verify dashboard tokens
# Optional multi-provider routing (JSON), see Routing & Providers in the app:
# PLINTH_PROXY_PROVIDERS=[{"name":"groq","base_url":"https://api.groq.com/openai","api_key":"${GROQ_API_KEY}"}]
```

Run: `uvicorn plinth_proxy.api:app --host 0.0.0.0 --port 7430` (or its container).

**Done when:**
```bash
curl https://app.plynf.com/v1/chat/completions \
  -H "Authorization: Bearer <your tenant key>" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'
```
returns a real completion (not a mock).

---

## 6. Turn on dashboard auth (5 min)

On the **dashboard** service set:
```
PLINTH_DASHBOARD_APP_AUTH_REQUIRED=true
PLINTH_DASHBOARD_IDENTITY_URL=http://identity:7425   # internal URL on your host
PLINTH_DASHBOARD_PROXY_URL=http://localhost:7430      # for live savings telemetry
PLINTH_DASHBOARD_CORS_ORIGINS=https://plynf.com,https://app.plynf.com
```
Restart it.

**Done when:** `curl https://app.plynf.com/api/app/state` returns **401** (good —
it now requires a token), and logging in via the site works (step 7).

---

## 7. Deploy the site (Netlify) (15 min)

1. Connect the repo to Netlify; base directory `landing`, build `npm run build`,
   publish `dist` (already in `landing/netlify.toml`).
2. Set Netlify environment variables:
   ```
   PUBLIC_PLYNF_API=https://app.plynf.com          # dashboard API (empty = same origin)
   PUBLIC_PLYNF_ENDPOINT=https://app.plynf.com/v1   # the LLM endpoint shown in Connect
   PUBLIC_PLYNF_BASE=https://app.plynf.com
   ```
3. Deploy. (The site currently has a Basic-Auth preview gate in
   `landing/netlify/edge-functions/auth.ts` — **delete that file + its block in
   `netlify.toml` when you go public**, or no one can see the site.)

**Done when:** `https://plynf.com/login` loads, signing in lands you in `/app`
with the **Live** badge, and the dashboard shows real numbers.

---

## 8. Deploy the OAuth broker (Cloudflare) (20 min)

```bash
cd apps/oauth-broker
npm i -g wrangler && wrangler login
wrangler kv namespace create STATE      # paste the returned id into wrangler.toml
wrangler secret put NOTION_CLIENT_SECRET # only providers that need a secret
wrangler deploy
```
Edit `wrangler.toml`: replace `GITHUB_CLIENT_ID` / `LINEAR_CLIENT_ID` /
`NOTION_CLIENT_ID` with the real values from step 9.

**Done when:** `curl https://oauth.plynf.com/health` returns `{"ok":true}`.

---

## 9. Register the OAuth apps (30 min) — for one-click tool connectors

For each provider, create an OAuth app and set the **redirect/callback URL** to:
`https://oauth.plynf.com/v1/oauth/cb`

| Provider | Where | Redirect URL |
|----------|-------|--------------|
| GitHub | github.com → Settings → Developer settings → OAuth Apps | `https://oauth.plynf.com/v1/oauth/cb` |
| Slack | api.slack.com/apps | same |
| Google | console.cloud.google.com → Credentials | same |
| Notion | notion.so/my-integrations | same |
| Linear | linear.app → Settings → API | same |
| Salesforce | Setup → App Manager → New Connected App | same |

Put the client IDs in `wrangler.toml`, secrets via `wrangler secret put …`.

**Done when:** clicking **Connect** on a tool in the dashboard completes the
OAuth round-trip.

---

## 10. Publish the marketplace integrations

Follow **`integrations/PUBLISHING.md`** (n8n, Zapier, Make, Copilot Studio).
After each goes live, send me the public URL and I flip the catalog link from
the GitHub source to the store (one line in `landing/src/data/platforms.ts`).

**Done when:** a listing is searchable in that platform's store.

---

## 11. Email, billing, partners

- **Email:** Cloudflare Email Routing → create `partners@plynf.com`,
  `support@plynf.com` → forward to your inbox.
- **Billing:** add Stripe keys to the proxy/identity when you want paid tiers
  live (until then, tiers are enforced but not charged).
- **Partners:** lead outreach with a dashboard screenshot of the savings + water
  numbers. Order: marketplace listings → ecosystem PRs → OpenAI/Anthropic
  partner programs (later, with traction). Don't ship their logos without
  written brand permission.

---

## 12. Final smoke test

```bash
curl https://app.plynf.com/healthz                 # ok
curl https://oauth.plynf.com/health                # {"ok":true}
# 401 without a token, 200 with a session token:
curl -o /dev/null -w "%{http_code}\n" https://app.plynf.com/api/app/state
```
Then in a browser: `plynf.com` → Log in → see the dashboard with the **Live**
badge, real KPIs, and the water tally. You're live.
