# Publishing the Plynf integrations

Turn the "real source" links in the Connect catalog (`landing/src/data/platforms.ts`)
into true marketplace one-click installs. Each listing requires a vendor account
and passes their review. After a listing goes live, send the URL and we swap the
catalog `href` from the GitHub source to the store.

Lead every listing with the same hook: **"Cut agent token cost ~70% — and see
the savings."** Attach a dashboard screenshot of the savings + water numbers.

---

## n8n — community node (`n8n-nodes-plynf/`)
1. `cd integrations/n8n-nodes-plynf && npm ci && npm run build`
2. Bump `version` in `package.json` (semver).
3. `npm login` (needs npm account + 2FA) → `npm publish --access public`.
4. It auto-appears in n8n once installed; request verification at
   <https://docs.n8n.io/integrations/community-nodes/> for the verified badge.
5. Catalog: set `n8n.href` → `https://www.npmjs.com/package/n8n-nodes-plynf`,
   `kind` stays `marketplace`, action → "Install in n8n".

## Zapier — app (`zapier-plynf/`)
1. Zapier Developer Platform account → create a private app.
2. `zapier push` (Zapier CLI) from the integration dir.
3. Internal test → invite testers → submit for **public** review
   (needs ~3 live users + brand assets + a privacy URL → use `/privacy`).
4. Catalog: set `zapier.href` → your public `https://zapier.com/apps/plynf/...`.

## Make — module (`make-plynf/`)
1. Make Developer Hub account → new app → import the module manifest.
2. Set base URL (`https://app.plynf.com`), connection (API key), and actions.
3. Submit for review → published to the Make marketplace.
4. Catalog: set `make.href` → your `https://www.make.com/en/integrations/plynf`.

## Microsoft Copilot Studio — custom connector (`copilot-studio-plynf/`)
1. Import the connector (Swagger/OpenAPI) into Power Platform.
2. Configure auth (API key) + host (`app.plynf.com`).
3. For broad availability, submit via the **Microsoft certified connector**
   program (longer review).
4. Catalog: point `copilot-studio.href` at the listing or your docs.

---

## Before any submission — the shared prerequisites
- [ ] `app.plynf.com` live (dashboard + API reachable over HTTPS).
- [ ] A privacy policy + terms URL (`/privacy`, `/terms` — already on the site).
- [ ] A support email (`support@plynf.com`) and `partners@plynf.com`.
- [ ] Brand assets: square icon (the Plynf mark), 1280×640 banner, 3–5
      screenshots (lead with the savings/water dashboard).
- [ ] A 1-line + 1-paragraph description (reuse the catalog `blurb`).
- [ ] At least one real connected account to demo in the review.

## Don't
- Don't ship third-party logos in our listings without their brand permission.
- Don't hardcode secrets in any published package — connections collect the key.
