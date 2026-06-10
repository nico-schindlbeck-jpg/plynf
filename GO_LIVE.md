# Go live — clicks only, no terminal

This runbook takes Plynf from the repo to a public, paying product. Every
step is done in a web browser. Do them top to bottom; each ends with
**Done when:** — don't move on until that's true.

The customer journey you're enabling:

> **Subscribe → key appears → paste key into agent → done.**
> (signup → Stripe Checkout → `/app/welcome` wizard → Test connection ✓)

---

## 0. The map (what serves what)

| URL | What it is | Hosted on |
|-----|-----------|-----------|
| `plynf.com` | Marketing site + dashboard UI + signup + savings preview | Netlify (static) |
| `api.plynf.com` | Control plane: signup, login, billing, Stripe webhook | Fly.io app **plynf-dashboard** |
| `app.plynf.com` | **The proxy** — customers point their agent's base_url here | Fly.io app **plynf-proxy** |
| (private) | Identity: session JWTs, key material | Fly.io app **plynf-identity** (no public IP) |

All three Fly apps live in region `fra` (Frankfurt — the "EU-hosted" claim
is real). Configs: `services/{proxy,dashboard,identity}/fly.toml`.
Customer LLM keys are **BYOK** (`x-plynf-upstream-key` header or
provider routing) — you pay no provider bills.

---

## 1. Deploy the backend (≈15 min, GitHub + Fly web UIs)

1. Create a **Fly.io** account at fly.io (free to start; add a credit card
   when prompted — three small VMs + two 1 GB volumes ≈ $5–8/month, and
   machines auto-stop when idle).
2. Fly Dashboard → **Tokens** (account menu) → *Create token* → name it
   `github-deploy`, copy the value.
3. GitHub → your repo → **Settings → Secrets and variables → Actions →
   New repository secret**: name `FLY_API_TOKEN`, value = the token.
4. GitHub → **Actions** tab → workflow **"Deploy to Fly.io"** → *Run
   workflow* (target: `all`). First run takes ~10 min: it creates the
   three apps + volumes, builds the images remotely, deploys, and smoke-
   checks the public health endpoints.

**Done when:** the workflow run is green and
`https://plynf-proxy.fly.dev/healthz` shows `"status":"ok"` in your browser.

---

## 2. Set the shared secret (5 min, Fly web UI)

The proxy resolves customer keys against the dashboard; they authenticate
to each other with one shared secret. Make up a long random string
(password manager → generate, 40+ chars), then:

1. Fly Dashboard → app **plynf-dashboard** → **Secrets** → *New secret*:
   `PLINTH_DASHBOARD_INTERNAL_SECRET` = the string.
2. Fly Dashboard → app **plynf-proxy** → **Secrets** → *New secret*:
   `PLINTH_PROXY_INTERNAL_SECRET` = the **same** string.

(Setting a secret restarts the app automatically.)

**Done when:** both apps show the secret in their Secrets list.

---

## 3. Point the domains (10 min, Fly + your DNS)

For each of the two public apps:

1. Fly Dashboard → app **plynf-proxy** → **Certificates** → *Add
   certificate* → `app.plynf.com`. Fly shows you the DNS records to add.
2. In your DNS provider (Cloudflare): add the shown **CNAME**
   `app` → `plynf-proxy.fly.dev` (DNS-only / grey cloud) plus the
   `_acme-challenge` record if shown.
3. Repeat for **plynf-dashboard** with `api.plynf.com` →
   `plynf-dashboard.fly.dev`.

**Done when:** both certificates show *Issued* in Fly, and
`https://api.plynf.com/healthz` + `https://app.plynf.com/healthz` load.

---

## 4. Publish the site (5 min, Netlify web UI)

The repo already contains everything (`landing/netlify.toml` bakes
`api.plynf.com` / `app.plynf.com` in at build time, and the old
preview-password gate has been removed).

1. Netlify → your site → **Deploys** → *Trigger deploy* (or just push to
   `main` if auto-deploy is on).

**Done when:** `https://plynf.com` loads **without** a password prompt,
and `/signup` renders the plan picker.

---

## 5. Stripe — real money (≈20 min, Stripe web UI)

Until this step, billing runs in **simulated mode** (signup + plan
switching work; nobody is charged). Flipping to real charges:

1. Create a **Stripe** account (stripe.com) and complete business
   verification.
2. **Product catalog → Add product**: name `Plynf Pro`, recurring,
   **$49 / month** (match `landing/src/data/plans.ts`). Copy the **price
   ID** (`price_…`).
3. Add a second product `Plynf Enterprise` (custom anchor price,
   recurring) and copy its price ID.
4. **Developers → Webhooks → Add endpoint**:
   - URL: `https://api.plynf.com/api/stripe/webhook`
   - Events: `checkout.session.completed`,
     `customer.subscription.updated`, `customer.subscription.deleted`
   - Copy the **signing secret** (`whsec_…`).
5. **Developers → API keys**: copy the **secret key** (`sk_live_…`).
6. Fly Dashboard → app **plynf-dashboard** → **Secrets** → add four:
   - `PLINTH_DASHBOARD_STRIPE_SECRET_KEY` = `sk_live_…`
   - `PLINTH_DASHBOARD_STRIPE_WEBHOOK_SECRET` = `whsec_…`
   - `PLINTH_DASHBOARD_STRIPE_PRICE_PRO` = `price_…` (Pro)
   - `PLINTH_DASHBOARD_STRIPE_PRICE_ENTERPRISE` = `price_…` (Enterprise)
7. Stripe → **Settings → Billing → Customer portal** → enable it (the
   "Manage billing" button in the welcome wizard uses it).

Tip: do a dry run first with Stripe **test mode** keys (`sk_test_…`,
test-mode webhook + prices) and card `4242 4242 4242 4242`, then swap the
four secrets for the live values.

**Done when:** a checkout started from `/signup?plan=pro` reaches Stripe's
payment page, and after paying, `/app/welcome` shows plan **Pro** within a
minute (the webhook flips it).

---

## 6. Prove the whole journey (5 min, incognito window)

1. `plynf.com` → Pricing → **Subscribe** (Pro).
2. Create account → pay (test card in test mode).
3. `/app/welcome`: step 1 turns green (plan Pro), step 2 shows your
   personal `plynf_sk_live_…` key pre-filled in the snippets, step 3
   **Test my connection** turns green.
4. Paste the two-line base_url change into any agent and watch
   `/app` show shaped calls + saved tokens.

**Done when:** all three wizard steps are green in a fresh browser.

---

## 7. After launch (optional, anytime)

- **Default upstream key**: set `PLINTH_PROXY_UPSTREAM_API_KEY` (Fly →
  plynf-proxy → Secrets) if you want requests without BYOK headers to work
  against your own OpenAI account. Otherwise customers bring their own —
  recommended, costs you nothing.
- **Durable savings analytics**: create a managed Postgres (Neon free
  tier, web UI) and set `PLINTH_PROXY_POSTGRES_URL` on plynf-proxy.
- **OAuth broker** (`oauth.plynf.com`, Cloudflare Worker) — only needed
  for one-click tool connectors; the core proxy journey works without it.
- **Marketplace listings** (n8n/Zapier/Make/bots) — parked per the
  engineering brief until the wedge has paying validation.
- **Status page / uptime checks**: point a free monitor (e.g.
  UptimeRobot) at the two `/healthz` URLs.

---

## Troubleshooting (still no terminal)

| Symptom | Fix |
|---------|-----|
| Workflow red at "Ensure volumes" | Re-run the workflow — Fly occasionally needs a second attempt right after app creation. |
| `/signup` says "could not reach the API" | Netlify deployed before DNS was ready — Trigger deploy again after step 3. |
| Welcome wizard stuck on "Checking your plan…" | plynf-dashboard secret `PLINTH_DASHBOARD_INTERNAL_SECRET` missing (step 2) or Stripe webhook URL typo (step 5.4). Check Fly → plynf-dashboard → Live Logs in the browser. |
| Test connection fails | Certificate for `app.plynf.com` not issued yet (step 3) — Fly → Certificates shows the pending DNS record. |
| Paid but plan stays Free | Stripe → Developers → Webhooks → the endpoint shows the delivery error message verbatim. |
