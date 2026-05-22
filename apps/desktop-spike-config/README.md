# Tauri Spike — Pattern A (iframe) verification

Tests whether Tauri 2.x can host an iframe pointing at `http://127.0.0.1:7420` under the default Content Security Policy (with one explicit `frame-src` allowlist entry). This is the central claim of ADR 0010 — once verified, Block 5 (Desktop App) can use this pattern without code-signing surprises from CSP-bypass flags.

## TL;DR

Three commands. Don't skip step 1.

```bash
# 1. Install Node (one-time, ~30s)
brew install node

# 2. Run our setup script (10 min — most of it is Rust compile time)
~/Code/plinth/apps/desktop-spike-config/setup.sh

# 3. Follow the printed commands at the end of step 2
```

The setup script generates a Tauri starter at `~/plynf-spike`, applies the iframe-friendly CSP, copies in a verification-aware index.html, and runs `npm install`.

## What you'll see

When everything works, the Tauri window shows the Plynf landing page (because that's what's served on :7420), with a small status overlay in the top-right corner:

- `… iframe:waiting · postMessage:waiting` — initial state
- `✓ iframe:loaded · postMessage:ok` — **Pattern A verified**
- `✘ iframe load timeout` — backend not running on :7420
- `✘ postMessage blocked: <message>` — CSP issue, would need Pattern B fallback

## What if it fails

Different failure modes mean different things:

| Symptom | Likely cause | Fix |
|---|---|---|
| Status stuck at "iframe:waiting" | Backend not on :7420 | Start `python3 -m http.server 7420` in `landing/dist/` |
| "iframe load timeout" | Backend not reachable from Tauri context | Check firewall, try `localhost` instead of `127.0.0.1` |
| CSP error in DevTools console (Cmd+Option+I) | Our CSP missing a directive | Screenshot the error, share, I'll widen `tauri.conf.json` |
| `npm run tauri dev` hangs forever | Rust still compiling | Wait 10 min. If still nothing, `cd src-tauri && cargo build` to see the actual progress |
| "command not found: cargo" | Rust missing | `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh` then `source $HOME/.cargo/env` |

## Files in this directory

| File | Purpose |
|---|---|
| `setup.sh` | One-shot installer + scaffolder. Idempotent. |
| `tauri.conf.json` | The Tauri config with the critical `frame-src` CSP rule. |
| `index.html` | iframe shim + JS verification harness (status overlay, postMessage round-trip). |
| `README.md` | This file. |

After the spike passes (or fails), the `~/plynf-spike` directory can be deleted. The files in this config directory stay — they document what worked for the eventual Block 5 implementation.

## Cleanup when done

```bash
rm -rf ~/plynf-spike
# Rust toolchain stays installed at ~/.rustup — needed for Block 5 anyway
```

## What this proves (or refutes)

**Pattern A (iframe)** vs Pattern B (WebView-Direct + asset protocol):

- Pattern A: Tauri main window has a 30-line shell HTML, iframe loads the dashboard from localhost. CORS not a problem because the dashboard is served by the dashboard service, not by Tauri's bundled assets. Easy to migrate the existing vanilla-JS dashboard to.
- Pattern B: Dashboard SPA is built into the Tauri bundle as static assets. Backend then needs CORS configured for the Tauri origin. Higher integration effort but tighter security model.

If this spike passes, Pattern A is chosen → ADR 0010 promoted from Proposed to Accepted → Block 5 estimates hold at 7 PT. If it fails, Pattern B becomes the fallback → Block 5 grows to ~9 PT because of the CORS + bundling work.
