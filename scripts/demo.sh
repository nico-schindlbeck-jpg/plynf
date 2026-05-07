#!/usr/bin/env bash
# Plinth — run the headline token-comparison demo.
# If services are running, they'll be used. Otherwise, simulation mode is used end-to-end (still produces real numbers).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV_PY="${ROOT}/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "✘ venv not found at $VENV_PY"
    echo "  Run: make install"
    exit 1
fi

TOPIC="${1:-renewable energy}"
MODE="${2:-simulation}"

echo
echo "▶ Plinth research-agent demo"
echo "  topic: $TOPIC"
echo "  mode:  $MODE"
echo

"$VENV_PY" "${ROOT}/examples/01-research-agent/compare.py" --topic "$TOPIC" --mode "$MODE"
