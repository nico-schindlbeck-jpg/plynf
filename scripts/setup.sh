#!/usr/bin/env bash
# Plinth — first-time setup: create venv, install everything, run smoke tests.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

echo -e "${GREEN}▶ Plinth setup${RESET}"
echo

# 1. python interpreter
PY=$(command -v python3.11 || command -v python3.12 || command -v python3.13 || command -v python3 || true)
if [ -z "$PY" ]; then
    echo -e "${RED}✘ No python3 found on PATH${RESET}"
    exit 1
fi
echo "  python: $PY ($($PY --version))"

# 2. venv
if [ ! -d ".venv" ]; then
    echo "  creating venv at .venv"
    $PY -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip wheel >/dev/null

# 3. install components
echo
echo -e "${GREEN}▶ Installing components${RESET}"
for pkg in "services/workspace[dev]" "services/gateway[dev]" "sdk/python[dev]" "examples/01-research-agent"; do
    if [ -d "${pkg%[*}" ]; then
        echo "  → $pkg"
        pip install -e "./$pkg" >/dev/null 2>&1 || pip install -e "./${pkg%[*}" >/dev/null
    fi
done

if [ -d "./mock-mcp-server" ]; then
    echo "  → mock-mcp-server"
    pip install -e "./mock-mcp-server[dev]" >/dev/null
fi

# 4. smoke tests
echo
echo -e "${GREEN}▶ Smoke tests${RESET}"
for svc in services/workspace services/gateway sdk/python; do
    if [ -d "./$svc" ]; then
        echo "  → pytest in $svc"
        (cd "$svc" && pytest -q --no-header --tb=short 2>&1 | tail -3)
    fi
done

echo
echo -e "${GREEN}✔ Setup complete${RESET}"
echo
echo "Next:"
echo "  make serve     # start services"
echo "  make demo      # run the headline demo"
echo "  make test      # full test suite"
