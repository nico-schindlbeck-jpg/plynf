# Go live — the exact steps

A precise, ordered runbook to take Plynf from "works on my laptop" to a public
product at **plynf.com**. Written for a non-developer: do the steps top to
bottom. The backend (step 4) is now **one command** — the proxy and automatic
HTTPS are bundled into the compose stack — so the only thing that wants a little
terminal comfort is pointing a domain at a server and running it (~30 min). The
managed Render/Fly path (step 4, Option B) needs even less. Everything else is
accounts, copy-paste, and clicks.

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

Everything behind `app.plynf.com` ships in **one** `deploy/compose.prod.yml`:
- **Runtime services**: dashboard (7424), identity (7425), gateway (7422),
  workspace (7421).
- **The proxy** — the token-shaping LLM front door SDKs point their base URL at.
- **Caddy** — the edge that terminates HTTPS and routes `/v1/*` + `/api/chat*`
  to the proxy and everything else to the dashboard. Certificates are fetched
  and renewed automatically; there's nothing to run by hand.

---

## 1. Accounts to create (15 min)

- [ ] A domain you control (**plynf.com**) at any registrar.
- [ ] **Netlify** account (free) — hosts the site.
- [ ] **Cloudflare** account (free) — hosts the OAuth broker + manages DNS (easiest).
- [ ] A host for the backend (pick one): **Hetzner/DigitalOcean VPS** (cheapest — one `docker compose up`) **or** **Render/Fly.io** (managed; deploy files already in `deploy/` + `services/proxy/`).
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

## 4. Put the backend online (one command)

The whole backend — runtime services, the **proxy**, and an automatic-HTTPS
**Caddy** edge — is one compose stack. There's no separate reverse proxy to
install and no certificates to manage.

**Option A — one small server (recommended, cheapest).**
1. Create a Linux server (Hetzner/DigitalOcean, 2 vCPU / 4 GB is plenty). Point
   `app.plynf.com`'s A record at its IP (step 2) and open ports **80 + 443**.
2. Install Docker, copy the repo (or just the `deploy/` folder), and make your
   env file from the template:
   ```bash
   cp deploy/prod.env.example deploy/.env
   nano deploy/.env     # set PLYNF_APP_DOMAIN + PLYNF_ACME_EMAIL now
                        # (the model/proxy vars come in step 5)
   ```
3. Bring it up:
   ```bash
   docker compose -f deploy/compose.prod.yml pull
   docker compose -f deploy/compose.prod.yml up -d --wait
   ```
   Caddy fetches and renews the TLS certificate for your domain automatically.

**Option B — managed (Render/Fly).** Use the deploy configs in `deploy/` and
`services/proxy/` (`render.yaml`, `fly.toml`) to create one service per
component — each gets its own HTTPS URL. Then map `app.plynf.com` to the
dashboard + proxy.

**Done when:** `curl https://app.plynf.com/healthz` returns `{"status":"ok"…}`
(allow Caddy ~30 s on first boot to issue the certificate).

---

## 5. Point the proxy at a real model (5 min)

The proxy is already running from step 4 — in **safe mock mode** until you give
it a model. To forward to a real upstream, fill these in the **same**
`deploy/.env` and re-apply:

```
PLINTH_PROXY_DEMO_MODE=false
PLINTH_PROXY_UPSTREAM_BASE_URL=https://api.openai.com
PLINTH_PROXY_UPSTREAM_API_KEY=<your OpenAI key>
PLINTH_PROXY_API_KEYS=<the tenant:key:tier from step 3>
```

The proxy's identity + gateway URLs are already wired internally — nothing else
to set. Re-apply with `docker compose -f deploy/compose.prod.yml up -d`.

**Done when:**
```bash
curl https://app.plynf.com/v1/chat/completions \
  -H "Authorization: Bearer <your tenant key>" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'
```
returns a real completion (not a mock).

---

## 6. Turn on dashboard auth (2 min)

The dashboard's internal URLs (identity/gateway/proxy) and CORS are already
wired in the compose stack. To require a login on the control plane, flip one
flag in `deploy/.env`:
```
PLINTH_DASHBOARD_APP_AUTH_REQUIRED=true
```
Re-apply: `docker compose -f deploy/compose.prod.yml up -d`.

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
