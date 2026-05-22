#!/usr/bin/env bash
# Tauri spike setup — one-shot from zero to "ready to run".
#
# Usage:
#   ~/Code/plinth/apps/desktop-spike-config/setup.sh
#
# What it does:
#   1. Verifies node + npm are installed (and Rust if available)
#   2. Generates a fresh Tauri starter at ~/plynf-spike (vanilla template)
#   3. Copies our pre-configured tauri.conf.json + index.html on top
#   4. Runs npm install (which also fetches Rust crates — slow first time)
#   5. Prints the exact two commands to start the spike
#
# If anything is already in place, it's preserved (idempotent).

set -euo pipefail

# ─── Colors ───────────────────────────────────────────────────────────
if [ -t 1 ]; then
    GREEN="$(printf '\033[32m')"
    YELLOW="$(printf '\033[33m')"
    RED="$(printf '\033[31m')"
    DIM="$(printf '\033[2m')"
    RESET="$(printf '\033[0m')"
else
    GREEN="" YELLOW="" RED="" DIM="" RESET=""
fi

step() { printf "\n${GREEN}→${RESET} %s\n" "$1"; }
warn() { printf "${YELLOW}⚠${RESET} %s\n" "$1"; }
fail() { printf "${RED}✘${RESET} %s\n" "$1"; exit 1; }

# ─── Resolve repo root (this script's location → up two) ──────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SPIKE_DIR="${HOME}/plynf-spike"

# ─── 1. Pre-flight checks ─────────────────────────────────────────────

step "Checking prerequisites"

if ! command -v node >/dev/null 2>&1; then
    fail "node not found.

  Install Node.js (which includes npm) first:

      brew install node                    # macOS via Homebrew
      curl -fsSL https://fnm.vercel.app/install | bash && fnm install --lts  # alternative

  Then re-run this script."
fi
echo "  ${DIM}node:${RESET}  $(node --version)"

if ! command -v npm >/dev/null 2>&1; then
    fail "npm not found — but node is? Reinstall Node to get npm."
fi
echo "  ${DIM}npm:${RESET}   $(npm --version)"

if command -v rustc >/dev/null 2>&1; then
    echo "  ${DIM}rustc:${RESET} $(rustc --version | cut -d' ' -f2)"
else
    warn "rustc not in PATH. Tauri will install Rust into ~/.cargo on first build (5-10 min)."
    warn "If you want to do it explicitly first:"
    warn "      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
fi

if [ ! -f "${SCRIPT_DIR}/tauri.conf.json" ]; then
    fail "tauri.conf.json not found at ${SCRIPT_DIR} — repo state is bad?"
fi

# ─── 2. Generate Tauri starter ────────────────────────────────────────

if [ -d "${SPIKE_DIR}" ]; then
    warn "${SPIKE_DIR} already exists — skipping create-tauri-app"
    warn "If you want a clean slate: rm -rf ${SPIKE_DIR} && re-run this script"
else
    step "Generating Tauri starter at ${SPIKE_DIR}"
    cd "${HOME}"
    # The create-tauri-app CLI dropped some flags between versions. The
    # safe form passes them after `--` so they reach the underlying tool.
    npm create tauri-app@latest -- \
        --manager npm \
        --template vanilla \
        --identifier dev.plynf.spike \
        --yes \
        plynf-spike \
        || fail "create-tauri-app failed. If you saw a prompt, re-run interactively:
      cd ~
      npm create tauri-app@latest
        Project name?       plynf-spike
        Identifier?         dev.plynf.spike
        Frontend language?  TypeScript / JavaScript (your choice)
        UI template?        Vanilla
        UI flavor?          (whatever — we replace src/index.html anyway)
      Then re-run this script — it will skip the generate step."
fi

# ─── 3. Apply our config ──────────────────────────────────────────────

step "Applying Plynf spike config"
cp "${SCRIPT_DIR}/tauri.conf.json" "${SPIKE_DIR}/src-tauri/tauri.conf.json"
echo "  ${DIM}wrote${RESET} ${SPIKE_DIR}/src-tauri/tauri.conf.json"
cp "${SCRIPT_DIR}/index.html"      "${SPIKE_DIR}/src/index.html"
echo "  ${DIM}wrote${RESET} ${SPIKE_DIR}/src/index.html"

# ─── 4. Install JS deps ───────────────────────────────────────────────

step "npm install (downloads Tauri-CLI and prepares Rust toolchain hooks)"
cd "${SPIKE_DIR}"
npm install --no-audit --no-fund

# ─── 5. Print run instructions ────────────────────────────────────────

cat <<EOF

${GREEN}✓${RESET} Setup complete. To run the spike:

  ${DIM}Terminal A (this one, after this script ends):${RESET}
      cd ${SPIKE_DIR}
      npm run tauri dev

  ${DIM}Terminal B (in a second tab/window):${RESET}
      cd ${REPO_ROOT}/landing/dist
      python3 -m http.server 7420

What you should see:
  - First run of 'npm run tauri dev' takes 5-10 min (Rust compiles its
    dependencies). Watch the terminal output. No error => keep waiting.
  - A native macOS window opens with the Plynf-landing iframe inside.
  - Top-right corner shows status: '✓ iframe:loaded · postMessage:ok'.
  - That confirmation = Pattern A (iframe) works. ADR 0010 can be
    promoted from Proposed to Accepted.

If something fails, screenshot it and share — I can diagnose CSP
errors or build issues from the output.

EOF
