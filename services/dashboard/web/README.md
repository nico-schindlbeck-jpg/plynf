# Plinth Dashboard — Web Build Pipeline

Vite + Preact + TypeScript build for new dashboard surfaces. Output lands in the dashboard service's static directory (`services/dashboard/src/plinth_dashboard/static/dist/`) so FastAPI serves it without any extra config.

## Why Vite + Preact

- **Preact** is a 4 KB React-API-compatible library. Existing JSX patterns work, bundle stays small. Matches the design constraint: dashboard ships inside the Tauri app bundle for Block 5; smaller is better.
- **Vite** for dev mode HMR and production builds. Multi-entry support lets us migrate the vanilla `app.js` route-by-route without forcing a big-bang rewrite.
- **TypeScript** in strict mode catches API contract drift between dashboard and backend services.

## Migration strategy

The existing `services/dashboard/src/plinth_dashboard/static/app.js` (4138 lines of vanilla JS) continues to serve `/` during the migration. New routes are added one by one as separate Vite entries:

| Route | Today | After Block 4 |
|---|---|---|
| `/` | vanilla `app.js` | vanilla `app.js` (unchanged) |
| `/welcome` | doesn't exist | this build → `dist/welcome.html` |
| `/settings` | vanilla | eventually moves into this build |
| `/tools` | vanilla | eventually moves into this build |

When all routes have migrated, `app.js` retires and the build output replaces it.

## Develop

```bash
cd services/dashboard/web
npm install          # first time only — installs preact, vite, typescript
npm run dev          # Vite at http://127.0.0.1:5173, /api proxied to :7424
```

The dashboard service must be running at `:7424` for the proxy to work. In another shell:

```bash
make serve-dashboard
```

Then open `http://127.0.0.1:5173/welcome.html`.

## Build for production

```bash
npm run build        # outputs to ../src/plinth_dashboard/static/dist/
```

The output is checked in to the dashboard service's pip package, so the FastAPI deployment ships the built bundle without a Node.js dependency. CI verifies that `npm run build` produces a clean diff against the checked-in `dist/` on every PR that touches `web/`.

## Add a new route

1. Create `welcome.html` sibling in `web/` (top-level entry)
2. Create `src/<name>-main.tsx` that mounts your Preact component
3. Add the entry to `vite.config.ts` under `build.rollupOptions.input`
4. Wire the dashboard FastAPI service to return the built HTML at the matching path

## Constraints

- **No SSR.** The dashboard ships in Tauri's WebView (Block 5) and over loopback HTTP (Compose/Embedded). Server-side rendering adds complexity for zero user benefit.
- **No state library by default.** Preact's hooks are sufficient for current scope. Add Zustand later only if a piece of state genuinely needs to live outside a component tree.
- **No router framework.** Each top-level HTML file is its own entry. If we ever need true client-side routing, `preact-router` is in deps.

## Type safety with backend

`src/lib/api.ts` is the single source of truth for what the dashboard expects from the backend. Schema drift surfaces as TypeScript errors at build time. Backend should ship matching Pydantic models and `openapi.json` regeneration on every Plinth release; a follow-up task imports those types via `openapi-typescript` if it becomes worthwhile.

## Status

This is Block 4's build-pipeline spike + Welcome wizard. Not yet wired into the dashboard service's routing — see `docs/distribution/CLICK_TO_INSTALL_GAPS.md` §1.1 for context.
