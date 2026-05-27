# Spike 0.2 — Tauri 2.x WebView Strategy

**Time-box**: 4h Eng + CI wait
**Branch**: `spike/0.2-tauri-webview`
**Output**: this spec + ADR 0010 (after manual verification)

## Question

Which of two patterns should the Plinth desktop app use to render the dashboard?

- **iframe-Pattern**: Tauri's main WebView loads `index.html` containing `<iframe src="http://127.0.0.1:7420">`. Backend serves the existing dashboard at its existing URL.
- **WebView-Direct**: Tauri's main WebView serves the dashboard SPA bundle directly from the app package (via Tauri's asset-protocol `tauri://localhost/`), and the SPA's JS makes `fetch('http://127.0.0.1:7420/api/...')` requests.

## What the Plan Cares About

| Property | Impact |
|---|---|
| Works out-of-the-box with default CSP on macOS, Windows, Linux | Block 5 schedule |
| Auto-update bundles size | User download/update bandwidth |
| Code-signing surface (how many native binaries to notarize) | Block 4.5 complexity |
| Backend CORS configuration burden | Affects every service |
| Dev experience (hot reload, browser devtools) | Iteration speed |

---

## Pattern A — iframe

### Setup

```
apps/desktop/src-tauri/tauri.conf.json (relevant excerpt)
{
  "app": {
    "windows": [{
      "url": "index.html",
      "title": "Plinth",
      "width": 1280,
      "height": 800
    }],
    "security": {
      "csp": "default-src 'self'; frame-src http://127.0.0.1:7420 http://localhost:7420; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' http://127.0.0.1:7420"
    }
  }
}
```

```html
<!-- apps/desktop/src/index.html -->
<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Plinth</title></head>
  <body style="margin:0">
    <iframe src="http://127.0.0.1:7420"
            style="width:100vw;height:100vh;border:0"></iframe>
  </body>
</html>
```

### Manual Verification Checklist

Run these commands once Rust is installed (`brew install rustup-init && rustup-init`):

```bash
cd apps/desktop
cargo install create-tauri-app --version "^4"
# Adopt the template into apps/desktop/
npm create tauri-app@latest -- --template vanilla --identifier dev.plinth.spike
cd plinth-spike

# Plug in the conf above, the index.html above, then in another shell:
python3 -m http.server 7420  # stand in for the dashboard

npm run tauri dev
```

Expect on each platform:

| Check | macOS | Win | Linux |
|---|---|---|---|
| iframe renders the localhost page | ? | ? | ? |
| postMessage from iframe to host works | ? | ? | ? |
| `window.__TAURI__` injection works inside iframe? | ⚠️ no, only main window | same | same |
| Default CSP needs adjustment | likely yes | likely yes | likely yes |
| Tauri DevTools shows iframe content | yes | yes | yes |

### Known Trade-offs

- **CSP `frame-src` widening**: Default Tauri CSP forbids cross-origin frames. We need to allowlist `http://127.0.0.1:7420`. This is a documented & supported config — it does NOT require `dangerousDisableAssetCspModification`.
- **No Tauri API access from iframe**: The iframe is a cross-origin context. `window.__TAURI__` is only injected into the main window, not the iframe. Solution: hosting page acts as a thin shim and exposes Tauri commands via `postMessage`. The dashboard SPA must use the `postMessage` channel for native features (file pickers, tray, autostart-toggle).
- **Backend on a fixed port**: localhost:7420 must always be that port. If a user already has something on 7420 the app fails. Mitigation: `plinth doctor` allocates a free port, writes it into `~/.plinth/state.json`, and the host page reads the port from a Tauri command at boot.

### Auto-Update Size Implication

iframe pattern means the Tauri WebView app is tiny: just `index.html` + a few KB of glue JS. The dashboard SPA is part of the backend (already shipped via the runtime binary). Updates of the dashboard happen by updating the backend, **not** by updating the desktop app. Desktop app updates only fire when Tauri itself, the CLI sidecar, or native shell features change.

**Estimated update payload**: 5-15 MB (Tauri wrapper + CLI sidecar).

---

## Pattern B — WebView-Direct (Asset-Protocol)

### Setup

```
apps/desktop/src-tauri/tauri.conf.json (relevant excerpt)
{
  "app": {
    "windows": [{
      "url": "index.html",
      "title": "Plinth",
      "width": 1280,
      "height": 800
    }],
    "security": {
      "csp": "default-src 'self'; connect-src 'self' http://127.0.0.1:7420 ipc: https://ipc.localhost; script-src 'self'; style-src 'self' 'unsafe-inline'"
    }
  },
  "build": {
    "frontendDist": "../dashboard-dist"
  }
}
```

The dashboard SPA is built (Vite + Preact per Block 4 decision) into `apps/desktop/dashboard-dist/`. Tauri serves `index.html` from there via the asset protocol. The SPA's JS makes `fetch('http://127.0.0.1:7420/api/...')` requests.

### Backend CORS

Every Plinth service that the dashboard talks to needs CORS allowlist for the Tauri origin:

```
macOS:    http://tauri.localhost
Windows:  https://tauri.localhost
Linux:    http://tauri.localhost
```

In FastAPI:

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://tauri.localhost",
        "https://tauri.localhost",
        "http://localhost:7420",  # dev mode (vite)
    ],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
```

### Known Trade-offs

- **Backend CORS becomes mandatory.** Today the dashboard is served by the dashboard service itself (same-origin), so CORS is irrelevant. With Pattern B, every service the SPA hits (workspace, gateway, identity) needs CORS configuration.
- **Dashboard ships with the desktop app, not the backend.** This decouples versions: desktop v1.6 + backend v1.7 is a possible matrix. The dashboard SPA must speak API v1 only, which it already does per the API-stability contract.
- **Asset-protocol behaviour differs across platforms.** macOS uses `tauri://localhost`, Windows uses `https://tauri.localhost`, Linux uses custom protocol handler. Tauri 2.x abstracts this but the `allow_origins` list must include all three forms.
- **No iframe sandbox boundary.** The SPA runs with full Tauri API access (`window.__TAURI__`). This is more powerful but also more attack surface — XSS in the dashboard would have native-shell access.

### Auto-Update Size Implication

Every desktop update includes the dashboard SPA bundle (estimated ~80–120 KB gzipped with Vite + Preact). Plus the Tauri wrapper + CLI sidecar.

**Estimated update payload**: 6–18 MB (slightly larger than Pattern A because SPA is bundled).

But: dashboard updates are now coupled to desktop releases. If the backend updates and the SPA doesn't follow, schema mismatches surface as runtime errors. Mitigation: API v1 contract is locked, but operator UX (e.g., new tabs for new features) only appears when the desktop ships too.

---

## Decision Matrix

| Criterion | Pattern A (iframe) | Pattern B (WebView-Direct) | Winner |
|---|---|---|---|
| Setup complexity | Lower — single index.html | Higher — Vite build into Tauri bundle | A |
| Backend CORS burden | Low (only dashboard service) | High (all services + per-platform origins) | A |
| Native Tauri API access from SPA | Indirect via postMessage shim | Direct via `window.__TAURI__` | B |
| Update payload size | Smaller | Slightly larger | A |
| Dashboard/desktop version coupling | Decoupled | Coupled | A (for v1.6, B is fine once cloud exists) |
| Attack surface | Cross-origin sandbox limits damage | Full Tauri API exposed to SPA bugs | A |
| Dev iteration (hot reload) | Standard browser devtools on localhost:7420 | Vite HMR through Tauri | tie |
| Migration path from current vanilla-JS dashboard | Zero — dashboard stays at :7420 | Significant — must build into bundle | A |
| Future-proofing for Cloud (where there's no localhost) | iframe to cloud URL still works | Asset-protocol incompatible with cloud — needs Pattern A anyway | A |

**9 of 10 criteria favour Pattern A.** Pattern B's only meaningful win (direct Tauri API access) is achievable in Pattern A via a documented postMessage protocol.

## Recommended Decision

**Pattern A — iframe.**

Concrete implementation rules:

1. Tauri main window loads `index.html` (a 30-line file) that resolves the backend port from a Tauri command at boot and renders `<iframe src="http://127.0.0.1:${PORT}">`.
2. CSP is configured to allow `frame-src` and `connect-src` for `http://127.0.0.1:*`. **No** `dangerousDisableAssetCspModification` flag.
3. The dashboard SPA continues to ship inside the dashboard service (no Vite build step into the Tauri bundle). The Block 4 build-pipeline change (Vite + Preact) happens INSIDE the dashboard service, served from `:7420`, transparent to Tauri.
4. Native features (open file, tray actions, auto-update notification) are exposed via a documented `window.parent.postMessage({ type: '...', payload: ... })` protocol. The Tauri host wraps these into Tauri commands and forwards results back via `iframe.contentWindow.postMessage`.
5. Port-allocation: `plinth-cli` picks a free port in 7420–7430 range at runtime, writes to `~/.plinth/state.json`. Tauri reads on boot.

## What Block 5 Still Has to Build

- The 30-line `index.html` shim.
- `tauri.conf.json` with the CSP above.
- Native-bridge module: `src-tauri/src/bridge.rs` exposing 6–10 Tauri commands (open_dashboard, doctor, start_runtime, stop_runtime, check_updates, configure_autostart, open_logs, quit, set_port).
- postMessage protocol contract documented at `docs/desktop/native-bridge.md`.
- Auto-update endpoint + signing setup (Block 4.5).
- Per-platform installer bundles + notarisation pipeline.

## Risks Still Open

1. **Apple notarisation can reject `frame-src http://127.0.0.1`** as "loads remote content". Mitigation: justification in the App Sandbox entitlement explaining the local-loopback use. Apple has approved similar patterns (Spotify, Plex desktop apps both use loopback). Confidence: medium-high.
2. **Windows SmartScreen rates loopback-iframe apps as low-trust** until reputation builds. Mitigation: Azure Trusted Signing (Block 4.5) provides signing chain that bypasses SmartScreen reputation-gathering phase.
3. **Linux distros vary** in WebView2/WebKitGTK behaviour with loopback iframes. Mitigation: Tauri 2.x abstracts this; both Pattern A and Pattern B share this risk equally.

## Action Required from the User

To convert this spec into a verified ADR, run the manual verification steps:

```bash
# Install Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source $HOME/.cargo/env

# Generate the Tauri template
cd /Users/nico/Code/plinth/apps
npm create tauri-app@latest -- --name desktop-spike --identifier dev.plinth.spike --template vanilla
cd desktop-spike

# Replace src-tauri/tauri.conf.json with Pattern A config from this spec
# Replace src/index.html with the iframe shim from this spec

# Start a fake backend on :7420
python3 -m http.server 7420 &

# Run Tauri dev mode
npm run tauri dev
```

Verify:
- [ ] iframe loads the localhost:7420 page on macOS
- [ ] No CSP warnings in DevTools console
- [ ] postMessage round-trip works (test with a hardcoded `window.postMessage({test: 1})` from inside the iframe)

If all three checks pass, this spec is promoted to **ADR 0010 (Accepted)**. If any fail, document the failure and we re-evaluate Pattern B.

## ETA from User

I cannot install Rust autonomously (sandbox prohibits `curl | sh`). The spike is therefore **half-complete**: the design analysis and decision matrix are done; the runtime verification needs ~30 min of your time once Rust is on the system.
