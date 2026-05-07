#!/usr/bin/env bash
# Plinth — health check for all services. Exit 0 if all healthy, 1 otherwise.
set -euo pipefail

WORKSPACE_PORT="${PLINTH_WORKSPACE_PORT:-7421}"
GATEWAY_PORT="${PLINTH_GATEWAY_PORT:-7422}"
MOCK_PORT="${PLINTH_MOCK_MCP_PORT:-7423}"
DASHBOARD_PORT="${PLINTH_DASHBOARD_PORT:-7424}"
IDENTITY_PORT="${PLINTH_IDENTITY_PORT:-7425}"
GITHUB_MCP_PORT="${PLINTH_GITHUB_MCP_PORT:-7426}"
SLACK_MCP_PORT="${PLINTH_SLACK_MCP_PORT:-7427}"
LINEAR_MCP_PORT="${PLINTH_LINEAR_MCP_PORT:-7428}"

GREEN="\033[32m"
RED="\033[31m"
YELLOW="\033[33m"
RESET="\033[0m"

ok=0
fail=0

check() {
    local name="$1" url="$2"
    if response=$(curl -sf --max-time 3 "$url" 2>/dev/null); then
        echo -e "  ${GREEN}✔${RESET} ${name}: ${response}"
        ok=$((ok+1))
    else
        echo -e "  ${RED}✘${RESET} ${name}: not reachable at ${url}"
        fail=$((fail+1))
    fi
}

echo "Plinth health check"
echo "──────────────────"
check "Workspace " "http://localhost:${WORKSPACE_PORT}/healthz"
check "Gateway   " "http://localhost:${GATEWAY_PORT}/healthz"
check "Mock MCP  " "http://localhost:${MOCK_PORT}/healthz"
check "Dashboard " "http://localhost:${DASHBOARD_PORT}/healthz"
check "Identity  " "http://localhost:${IDENTITY_PORT}/healthz"
check "GitHub MCP" "http://localhost:${GITHUB_MCP_PORT}/healthz"
check "Slack MCP " "http://localhost:${SLACK_MCP_PORT}/healthz"
check "Linear MCP" "http://localhost:${LINEAR_MCP_PORT}/healthz"
echo "──────────────────"
echo "  ${ok} ok, ${fail} failing"

if [ "$fail" -gt 0 ]; then
    echo -e "${YELLOW}Tip:${RESET} services not running? Try \`make serve\`."
    exit 1
fi
