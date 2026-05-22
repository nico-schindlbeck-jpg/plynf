# Plynf dashboard — web SPA

Vite + Preact frontend that ships with the dashboard FastAPI service. Replaces (incrementally) the existing 4,138-line vanilla JS file at `src/plinth_dashboard/static/app.js`.

## Why Preact, not React or Vanilla

- **Vite + Preact**: 40 KB bundle, React-compatible API. JSX productivity, hooks, ecosystem.
- React would be 130 KB for the same shape — not worth 3× the bytes for a dashboard with under 30 components.
- Vanilla JS scaled fine for the demo phase but the wizard alone needs routing, form state, and async API calls — point at which a framework saves more code than it costs.
- Astro Islands is great for marketing sites but for an interactive dashboard with real-time data, an SPA framework is the right tool.

Decision tracked under Block C, question C2 in the open-tasks doc.

## Quick start

```bash
cd services/dashboard/web
npm install
npm run dev          # Vite dev server on :5173, proxies /api → :7424
```

Open `http://localhost:5173` — hot reload is on, edit a `.tsx` file and the browser updates.

## Production build

```bash
npm run build        # outputs to ../src/plinth_dashboard/static_vite/
```

The dashboard FastAPI service serves `static_vite/` at `/` in production. During the migration window, the old `static/app.js` still exists at `/legacy` — flag-protected by `?legacy=1` query param.

## Routes

| Path | Component | Status |
|---|---|---|
| `/welcome` | `routes/welcome.tsx` | 3-step first-run wizard. **Done.** |
| `/welcome/:step` | same | Deep-link to a wizard step. **Done.** |
| `/` | `routes/overview.tsx` (or redirects to welcome) | Workspace list. **Stub.** |
| `/workspaces/:id` | TODO | Per-workspace detail. **Stub.** |
| `/tools` | TODO | Tool inventory, OAuth connect. **Pending Block 7a.** |

## What ships in this scaffold

- Root `app.tsx` with tenant-probe → wizard-or-overview routing logic
- 3-step welcome wizard with form validation, error display, API-key one-time-display
- Overview stub showing workspaces + the sample-task-card highlighted via `?ftux=run-sample`
- Brand-coherent styles in `styles.css` (same tokens as the marketing site)
- Typed API wrapper in `lib/api.ts` with proper error shapes

## What's not in this scaffold

- Real-time updates (server-sent events or polling) — comes with the workspaces view in next PR
- Workspaces/tools/audit views — incremental, file per route
- Tests (vitest is wired but no test files yet)
- Dashboard backend `/api/v1/bootstrap` endpoint — pending Block 4 backend PR in `services/dashboard/`

## Migration strategy

The existing `static/app.js` (4,138 lines, vanilla JS) is **not** deleted. It stays reachable at `/legacy` so users / contributors can A/B compare. As each route gets ported here, we drop the corresponding chunk from `app.js`. When `app.js` is down to <500 lines, we delete the legacy route.

## Build size targets

| Target | Goal | Gzipped |
|---|---:|---:|
| Initial bundle (vendor split) | ≤ 60 KB | ≤ 20 KB |
| Per-route lazy chunks | ≤ 20 KB | ≤ 7 KB |
| Total assets including CSS + fonts | ≤ 200 KB | ≤ 70 KB |

If we drift above these, that's a feature-creep signal worth investigating.
