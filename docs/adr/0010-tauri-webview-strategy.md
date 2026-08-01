# ADR 0010: Tauri WebView Strategy (iframe vs Asset-Protocol)

- **Status**: Proposed (awaiting runtime verification — see `spike/tauri_webview_spec.md`)
- **Date**: 2026-05-15
- **Deciders**: The Plinth Authors
- **Related**: ADR 0009 (Embedded Lifespan), upcoming ADR 0011 (OAuth Broker)

## Context

Block 5 of the Distribution roadmap ships a Tauri 2.x desktop app that bundles the Plinth runtime and exposes the existing dashboard. Two viable rendering patterns exist:

- **A (iframe)**: Tauri main window hosts an iframe pointing at `http://127.0.0.1:7420`, where the dashboard service serves its existing SPA.
- **B (Asset-protocol)**: The dashboard SPA is built into the Tauri bundle and served via `tauri://localhost/`, with cross-origin `fetch()` calls to backend services.

The choice has downstream consequences for CORS configuration in every Plinth service, the desktop auto-update bundle size, the dashboard build pipeline (Block 4), and Apple/Microsoft notarisation review surface.

## Spike

`spike/tauri_webview_spec.md` runs both patterns through a 10-criterion decision matrix covering setup complexity, CORS burden, native API access, update payload, version coupling, attack surface, dev iteration, migration cost, and future-proofing for the Cloud product.

Result: **9 of 10 criteria favour Pattern A**. Pattern B's only meaningful advantage (direct `window.__TAURI__` access from the SPA) is achievable in A via a documented `postMessage` protocol that wraps Tauri commands.

The spike's runtime verification (does the iframe pattern actually work under default Tauri 2.x CSP on macOS/Win/Linux) is blocked by the autonomous environment's inability to install a Rust toolchain. The design-stage conclusion is high-confidence based on Tauri 2.x docs, the precedent set by Spotify/Plex desktop apps using loopback iframes, and the explicit Tauri CSP `frame-src` directive (which does not require any "dangerous" override).

## Decision

**Adopt Pattern A — iframe loading the existing dashboard at `http://127.0.0.1:${PORT}`.**

Rules:

1. The desktop app's main window loads `apps/desktop/src/index.html`, a ~30-line shim. It resolves the runtime backend port from a Tauri command at boot, then renders `<iframe src="http://127.0.0.1:${PORT}">`.
2. `tauri.conf.json` CSP allows `frame-src http://127.0.0.1:*` and `connect-src http://127.0.0.1:*`. **No** `dangerousDisableAssetCspModification` flag.
3. The dashboard SPA continues to live in `services/dashboard/` and is served at `:7420`. The Block 4 build-pipeline change (Vite + Preact) happens **inside the dashboard service**, not into the Tauri bundle.
4. Native features (open file dialog, tray actions, auto-update notification, autostart toggle) are exposed via a documented `window.parent.postMessage({type, payload})` protocol. The Tauri host translates these to Tauri commands and forwards results back via `iframe.contentWindow.postMessage`.
5. Port allocation: `plinth-cli` selects a free port in the 7420–7430 range at runtime and persists it to `~/.plinth/state.json`. Tauri reads on boot and passes to the shim.

## Consequences

### Positive

- **Zero CORS configuration in services.** The dashboard service serves both the SPA and the API; same-origin from the iframe's perspective. Workspace, gateway, identity remain unchanged.
- **Decoupled versioning.** Backend updates and desktop updates can ship independently. The iframe always reflects whatever the dashboard service serves; no SPA-in-Tauri version drift.
- **Smaller auto-update payload** (estimated 5–15 MB vs 6–18 MB for Pattern B). The dashboard SPA is updated by the backend update cycle, not the desktop cycle.
- **Smaller attack surface.** The iframe is a cross-origin sandbox; XSS in the dashboard does not get `window.__TAURI__` access. Native operations go through an explicit postMessage allowlist that the host validates.
- **Future-proofing for Cloud.** When the hosted product ships (`app.plinth.dev`), the desktop app can point the iframe at the cloud URL with no code change. Pattern B would require a fork of the rendering strategy.
- **Survives the existing vanilla-JS dashboard.** No migration is required to ship Block 5; Block 4's Vite + Preact migration happens inside the dashboard service at its own pace.

### Negative

- **postMessage indirection for native features.** Every native action (open log file, toggle autostart, check for update) is two hops: SPA → postMessage → Tauri host → Tauri command → return value via postMessage. Slightly more code, debuggable through DevTools.
- **Port allocation must be robust.** If 7420 is taken, the CLI must pick another port AND the desktop shim must learn it before rendering the iframe. Failure mode: blank white window. Mitigation: shim shows a loading state with `plinth doctor` summary if backend isn't reachable within 30s.
- **Apple notarisation review may question loopback iframe.** Spotify and Plex have precedent, but if Apple rejects on the first submission, we need to add an explanation in the entitlement justification. Low-probability, well-trodden path.

### Neutral

- The dashboard service must serve relaxed CSP for being framed (`X-Frame-Options: SAMEORIGIN` is too strict for the Tauri origin). We set `Content-Security-Policy: frame-ancestors http://tauri.localhost https://tauri.localhost http://localhost:*` on dashboard responses.
- DevTools in Tauri dev mode shows the iframe content with full inspector access, equivalent to a browser dev experience.

## Alternatives Considered

### Pattern B — Asset-protocol with Vite-built SPA in bundle

Rejected per the 10-criterion matrix in the spike spec. Pattern B's only meaningful win is direct Tauri API access from the SPA, which we obtain via postMessage in Pattern A without sacrificing the other 9 criteria.

### Tauri WebView with `dangerousDisableAssetCspModification: true`

Rejected. Disabling the asset CSP modification is a known security regression and is flagged in Tauri's audit guidance. It also signals to Apple/Microsoft reviewers that the app is bypassing security controls, increasing notarisation risk.

### Electron instead of Tauri

Out of scope for this ADR but worth recording: Electron was considered and rejected in an earlier informal review because (a) bundle size 3–5× larger than Tauri (~150 MB vs ~30 MB base), (b) Chromium update cadence forces frequent Electron releases, (c) Tauri's Rust-native command system is cleaner than Electron's IPC bridge. ADR pending if the decision is ever revisited.

## Open Items Before Promotion to Accepted

1. **Runtime verification on macOS/Win/Linux** — Section "Action Required from the User" in `spike/tauri_webview_spec.md`. Once `npm run tauri dev` is verified end-to-end with the iframe pattern and CSP above, this ADR moves to Status: Accepted.
2. **postMessage protocol contract** — to be drafted as part of Block 5 in `docs/desktop/native-bridge.md`. Approximately 8 commands: `get_runtime_port`, `start_runtime`, `stop_runtime`, `doctor`, `check_for_updates`, `set_autostart`, `open_logs`, `quit`.
3. **Port-conflict UX** — to be designed in Block 5 along with the Tauri shim. Spec: 5s probe loop on `:7420`, then 7421, then 7422, …, up to 7430. If all 11 ports are taken, the splash screen surfaces the conflict with a copyable `plinth doctor --json` output.

## Effort Implication

Pattern A keeps Block 5 estimate at **7 PT** (no change from v2-plan). Pattern B would have added ~1 PT for service-side CORS work and Vite-into-Tauri build integration — sticking with A avoids that growth.
