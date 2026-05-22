#!/bin/sh
# Plinth installer — Stufe 1 (one-liner)
# Usage:
#   curl -fsSL https://plynf.com/install.sh | sh
#   curl -fsSL https://plynf.com/install.sh | sh -s -- --verbose
#
# Flags:
#   --verbose          Print every step (default: quiet)
#   --skip-autostart   Skip launchd/systemd unit installation
#   --skip-services    Skip pip installs (useful for tests)
#   --skip-open        Don't open dashboard at the end
#   --no-update        Don't `git pull` if repo already exists
#   --dry-run          Print actions but make no changes
#   --help             Show this help
#
# Environment overrides:
#   PLINTH_HOME        Install root          (default: $HOME/.plinth)
#   PLINTH_BIN_DIR     CLI install dir       (default: $HOME/.local/bin)
#   PLINTH_REPO_URL    Git remote            (default: https://github.com/nico-schindlbeck-jpg/plinth)
#   PLINTH_REF         Branch/tag/sha        (default: main)
#   PLINTH_PYTHON      Python interpreter    (default: python3)
#
# This script is POSIX sh — no bashisms. Idempotent — re-running is safe.
# No sudo required when using default paths.

set -eu

# ───────── Defaults ─────────
PLINTH_HOME="${PLINTH_HOME:-$HOME/.plinth}"
PLINTH_BIN_DIR="${PLINTH_BIN_DIR:-$HOME/.local/bin}"
PLINTH_REPO_URL="${PLINTH_REPO_URL:-https://github.com/nico-schindlbeck-jpg/plinth}"
PLINTH_REF="${PLINTH_REF:-main}"
PLINTH_PYTHON="${PLINTH_PYTHON:-python3}"

VERBOSE=0
SKIP_AUTOSTART=0
SKIP_SERVICES=0
SKIP_OPEN=0
NO_UPDATE=0
DRY_RUN=0

# Marker strings for the PATH-block in shell rc files (used by uninstall).
RC_MARKER_START="# >>> Plinth installer >>>"
RC_MARKER_END="# <<< Plinth installer <<<"

# ANSI — disabled when stdout is not a TTY.
if [ -t 1 ]; then
    C_DIM="$(printf '\033[2m')"
    C_BOLD="$(printf '\033[1m')"
    C_GREEN="$(printf '\033[32m')"
    C_YELLOW="$(printf '\033[33m')"
    C_RED="$(printf '\033[31m')"
    C_RESET="$(printf '\033[0m')"
else
    C_DIM=""
    C_BOLD=""
    C_GREEN=""
    C_YELLOW=""
    C_RED=""
    C_RESET=""
fi

# ───────── Logging helpers ─────────
info() {
    printf '%s\n' "$*"
}

debug() {
    if [ "$VERBOSE" -eq 1 ]; then
        printf '%s%s%s\n' "$C_DIM" "$*" "$C_RESET"
    fi
}

warn() {
    printf '%s%s%s\n' "$C_YELLOW" "$*" "$C_RESET" >&2
}

err() {
    printf '%s%s%s\n' "$C_RED" "$*" "$C_RESET" >&2
}

die() {
    err "error: $*"
    exit 1
}

has() {
    command -v "$1" >/dev/null 2>&1
}

run() {
    # Run command, respecting --dry-run + --verbose.
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '%s+ %s%s\n' "$C_DIM" "$*" "$C_RESET"
        return 0
    fi
    if [ "$VERBOSE" -eq 1 ]; then
        printf '%s+ %s%s\n' "$C_DIM" "$*" "$C_RESET"
    fi
    "$@"
}

# Parse args -----------------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --verbose|-v)        VERBOSE=1 ;;
            --skip-autostart)    SKIP_AUTOSTART=1 ;;
            --skip-services)     SKIP_SERVICES=1 ;;
            --skip-open)         SKIP_OPEN=1 ;;
            --no-update)         NO_UPDATE=1 ;;
            --dry-run)           DRY_RUN=1 ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                die "Unknown argument: $1 (try --help)"
                ;;
        esac
        shift
    done
}

usage() {
    cat <<'EOF'
Plinth installer — Stufe 1 (one-liner)
Usage:
  curl -fsSL https://plynf.com/install.sh | sh
  curl -fsSL https://plynf.com/install.sh | sh -s -- --verbose

Flags:
  --verbose          Print every step (default: quiet)
  --skip-autostart   Skip launchd/systemd unit installation
  --skip-services    Skip pip installs (useful for tests)
  --skip-open        Don't open dashboard at the end
  --no-update        Don't `git pull` if repo already exists
  --dry-run          Print actions but make no changes
  --help             Show this help

Environment overrides:
  PLINTH_HOME        Install root          (default: $HOME/.plinth)
  PLINTH_BIN_DIR     CLI install dir       (default: $HOME/.local/bin)
  PLINTH_REPO_URL    Git remote            (default: https://github.com/nico-schindlbeck-jpg/plinth)
  PLINTH_REF         Branch/tag/sha        (default: main)
  PLINTH_PYTHON      Python interpreter    (default: python3)

This script is POSIX sh — no bashisms. Idempotent — re-running is safe.
No sudo required when using default paths.
EOF
}

banner() {
    cat <<EOF
${C_BOLD}Plinth installer${C_RESET}
${C_DIM}─────────────────${C_RESET}
  Install dir : $PLINTH_HOME
  CLI dir     : $PLINTH_BIN_DIR
  Git remote  : $PLINTH_REPO_URL
  Ref         : $PLINTH_REF
EOF
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  %sDry-run mode — no changes will be made%s\n' "$C_YELLOW" "$C_RESET"
    fi
    info ""
}

# ───────── Platform detection ─────────
detect_platform() {
    OS="$(uname -s)"
    case "$OS" in
        Darwin) PLATFORM="macos" ;;
        Linux)  PLATFORM="linux" ;;
        *)      die "unsupported OS '$OS' — Plinth installs on macOS and Linux only." ;;
    esac
    debug "Platform detected: $PLATFORM"
}

# ───────── Prerequisites ─────────
check_prereqs() {
    debug "Checking prerequisites..."
    missing=""
    has git    || missing="$missing git"
    has curl   || missing="$missing curl"
    has tar    || missing="$missing tar"
    has "$PLINTH_PYTHON" || missing="$missing $PLINTH_PYTHON"

    if [ -n "$missing" ]; then
        err "missing required tools:$missing"
        err ""
        err "On macOS: install Xcode CLT (xcode-select --install) and python3 from python.org"
        err "On Linux: apt-get install git curl python3 python3-venv  (or your distro's equiv.)"
        exit 1
    fi

    # Python >= 3.11
    py_ok=$("$PLINTH_PYTHON" -c \
        'import sys; print("1" if sys.version_info[:2] >= (3, 11) else "0")' 2>/dev/null || echo 0)
    if [ "$py_ok" != "1" ]; then
        py_ver=$("$PLINTH_PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo "unknown")
        if [ "$DRY_RUN" -eq 1 ]; then
            warn "Python 3.11+ required (found $py_ver). --dry-run continues anyway."
        else
            die "Python 3.11+ required (found $py_ver). Install from https://python.org or use pyenv."
        fi
    fi

    # venv module
    if ! "$PLINTH_PYTHON" -c 'import venv' >/dev/null 2>&1; then
        if [ "$DRY_RUN" -eq 1 ]; then
            warn "Python 'venv' module missing. --dry-run continues anyway."
        else
            die "Python 'venv' module missing. On Debian/Ubuntu: sudo apt-get install python3-venv"
        fi
    fi

    debug "Prerequisites OK"
}

# ───────── Stop existing install (if any) ─────────
stop_existing() {
    if [ -x "$PLINTH_BIN_DIR/plinth" ]; then
        debug "Stopping any running Plinth services..."
        run "$PLINTH_BIN_DIR/plinth" stop >/dev/null 2>&1 || true
    fi
    if [ "$PLATFORM" = "macos" ] && [ -f "$HOME/Library/LaunchAgents/dev.plinth.services.plist" ]; then
        run launchctl unload "$HOME/Library/LaunchAgents/dev.plinth.services.plist" >/dev/null 2>&1 || true
    fi
    if [ "$PLATFORM" = "linux" ] && has systemctl; then
        run systemctl --user stop plinth.service >/dev/null 2>&1 || true
    fi
}

# ───────── Fetch repo ─────────
fetch_repo() {
    repo_dir="$PLINTH_HOME/repo"
    if [ -d "$repo_dir/.git" ]; then
        if [ "$NO_UPDATE" -eq 1 ]; then
            info "Repo present (skipping update — --no-update)"
            return 0
        fi
        info "Updating existing Plinth install..."
        run git -C "$repo_dir" fetch --depth 1 origin "$PLINTH_REF" --quiet
        run git -C "$repo_dir" reset --hard "FETCH_HEAD" --quiet
    else
        info "Downloading Plinth..."
        run mkdir -p "$PLINTH_HOME"
        run git clone --depth 1 --branch "$PLINTH_REF" \
            "$PLINTH_REPO_URL" "$repo_dir" --quiet
    fi
}

# ───────── Create state dirs ─────────
ensure_state_dirs() {
    run mkdir -p "$PLINTH_HOME/state/data" \
                 "$PLINTH_HOME/state/logs" \
                 "$PLINTH_HOME/state/pids"
}

# ───────── Python venv + services ─────────
setup_venv() {
    venv="$PLINTH_HOME/venv"
    repo="$PLINTH_HOME/repo"

    if [ ! -d "$venv" ]; then
        info "Creating Python venv..."
        run "$PLINTH_PYTHON" -m venv "$venv"
    fi

    run "$venv/bin/pip" install --upgrade pip wheel --quiet --disable-pip-version-check

    if [ "$SKIP_SERVICES" -eq 1 ]; then
        info "Skipping service installs (--skip-services)"
        return 0
    fi

    info "Installing services (~60s)..."
    # Each pkg in its own pip call so a single failure is easy to diagnose.
    install_one() {
        spec="$1"
        label="$2"
        path="${spec%%[*}"           # strip '[dev]' to test path existence
        if [ ! -d "$repo/$path" ]; then
            debug "  (skip $label — $repo/$path not in checkout)"
            return 0
        fi
        debug "  -> $label"
        run "$venv/bin/pip" install --quiet --disable-pip-version-check \
            -e "$repo/$spec" \
            || die "pip install failed for $label (see ~/.plinth/install.log for details)"
    }

    install_one "services/workspace[dev]"        "workspace"
    install_one "services/gateway[dev]"          "gateway"
    install_one "services/identity[dev]"         "identity"
    install_one "services/dashboard[dev]"        "dashboard"
    install_one "mock-mcp-server[dev]"           "mock-mcp"
    install_one "mcp-servers/github[dev]"        "github-mcp"
    install_one "mcp-servers/slack[dev]"         "slack-mcp"
    install_one "mcp-servers/linear[dev]"        "linear-mcp"
    install_one "sdk/python[dev]"                "sdk-python"
    install_one "examples/01-research-agent"     "demo-01"
}

# ───────── Install plinth CLI wrapper ─────────
install_cli() {
    info "Installing plinth CLI..."
    run mkdir -p "$PLINTH_BIN_DIR"
    src="$PLINTH_HOME/repo/install/plinth"
    dst="$PLINTH_BIN_DIR/plinth"
    if [ "$DRY_RUN" -ne 1 ] && [ ! -f "$src" ]; then
        die "CLI wrapper not found at $src — repo checkout may be incomplete"
    fi
    run cp "$src" "$dst"
    run chmod +x "$dst"
    ensure_path_includes
}

ensure_path_includes() {
    # If $PLINTH_BIN_DIR is already in PATH, nothing to do.
    case ":$PATH:" in
        *":$PLINTH_BIN_DIR:"*) debug "PATH already includes $PLINTH_BIN_DIR"; return 0 ;;
    esac

    # Pick the right rc file. Honour the user's current shell where possible.
    rc=""
    case "${SHELL:-}" in
        */zsh)  rc="$HOME/.zshrc" ;;
        */bash) rc="$HOME/.bashrc" ;;
        *)      rc="$HOME/.profile" ;;
    esac
    [ -n "$rc" ] || rc="$HOME/.profile"

    # Already patched?
    if [ -f "$rc" ] && grep -Fq "$RC_MARKER_START" "$rc" 2>/dev/null; then
        debug "PATH block already present in $rc"
        return 0
    fi

    info "Adding $PLINTH_BIN_DIR to PATH (via $rc)"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '%s+ append PATH block to %s%s\n' "$C_DIM" "$rc" "$C_RESET"
        return 0
    fi
    {
        printf '\n%s\n' "$RC_MARKER_START"
        printf '# This block was added by the Plinth installer. To remove it, run\n'
        printf '#   plinth uninstall\n'
        printf '# (or delete the block manually).\n'
        printf 'export PATH="%s:$PATH"\n' "$PLINTH_BIN_DIR"
        printf '%s\n' "$RC_MARKER_END"
    } >> "$rc"

    warn "  → open a new shell or run: source $rc"
}

# ───────── Auto-start (launchd / systemd) ─────────
install_autostart() {
    if [ "$SKIP_AUTOSTART" -eq 1 ]; then
        info "Skipping auto-start install (--skip-autostart)"
        return 0
    fi
    case "$PLATFORM" in
        macos) install_launchd ;;
        linux) install_systemd ;;
    esac
}

install_launchd() {
    info "Installing launchd agent..."
    src="$PLINTH_HOME/repo/install/launchd/dev.plinth.services.plist.template"
    dst="$HOME/Library/LaunchAgents/dev.plinth.services.plist"
    if [ "$DRY_RUN" -ne 1 ] && [ ! -f "$src" ]; then
        die "launchd template not found: $src"
    fi

    run mkdir -p "$HOME/Library/LaunchAgents"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '%s+ render %s -> %s%s\n' "$C_DIM" "$src" "$dst" "$C_RESET"
    else
        # POSIX sed -i: macOS needs an explicit suffix. We write to a tmpfile
        # then mv into place so the resulting file is always atomic.
        tmp="$(mktemp 2>/dev/null || mktemp -t plinth)"
        sed -e "s|__HOME__|$HOME|g" \
            -e "s|__VENV__|$PLINTH_HOME/venv|g" \
            -e "s|__REPO__|$PLINTH_HOME/repo|g" \
            -e "s|__PLINTH_HOME__|$PLINTH_HOME|g" \
            "$src" > "$tmp"
        mv "$tmp" "$dst"
    fi
    run launchctl unload "$dst" >/dev/null 2>&1 || true
    run launchctl load -w "$dst"
    info "  ✔ launchd agent loaded (will auto-start on login)"
}

install_systemd() {
    if ! has systemctl; then
        warn "systemctl not found — skipping auto-start (services can still be run manually)"
        return 0
    fi
    info "Installing systemd --user unit..."
    src="$PLINTH_HOME/repo/install/systemd/plinth.service.template"
    dst="$HOME/.config/systemd/user/plinth.service"
    if [ "$DRY_RUN" -ne 1 ] && [ ! -f "$src" ]; then
        die "systemd template not found: $src"
    fi

    run mkdir -p "$HOME/.config/systemd/user"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '%s+ render %s -> %s%s\n' "$C_DIM" "$src" "$dst" "$C_RESET"
    else
        tmp="$(mktemp 2>/dev/null || mktemp -t plinth)"
        sed -e "s|__HOME__|$HOME|g" \
            -e "s|__VENV__|$PLINTH_HOME/venv|g" \
            -e "s|__REPO__|$PLINTH_HOME/repo|g" \
            -e "s|__PLINTH_HOME__|$PLINTH_HOME|g" \
            "$src" > "$tmp"
        mv "$tmp" "$dst"
    fi
    run systemctl --user daemon-reload
    run systemctl --user enable --now plinth.service
    # On some Linux distros, lingering must be enabled for --user units to
    # survive logout. We don't enable it here (it needs sudo) but we warn.
    if has loginctl && ! loginctl show-user "$USER" 2>/dev/null | grep -Fq 'Linger=yes'; then
        warn "  Tip: to keep Plinth running across reboots/logouts, run:"
        warn "       sudo loginctl enable-linger $USER"
    fi
    info "  ✔ systemd unit enabled"
}

# ───────── Wait for services ─────────
wait_for_services() {
    if [ "$SKIP_SERVICES" -eq 1 ] || [ "$SKIP_AUTOSTART" -eq 1 ]; then
        debug "Skipping wait-for-services (services not started)"
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        return 0
    fi
    info "Waiting for services to come up..."
    n=0
    max=45
    while [ "$n" -lt "$max" ]; do
        if curl -sf --max-time 2 http://localhost:7421/healthz >/dev/null 2>&1 \
            && curl -sf --max-time 2 http://localhost:7424/healthz >/dev/null 2>&1; then
            info "  ✔ Services healthy"
            return 0
        fi
        n=$((n + 1))
        sleep 1
    done
    warn "  ! Services did not become healthy in ${max}s."
    warn "    Check logs: tail -f $PLINTH_HOME/state/logs/*.log"
    return 0
}

# ───────── Open dashboard ─────────
open_dashboard() {
    if [ "$SKIP_OPEN" -eq 1 ] || [ "$DRY_RUN" -eq 1 ]; then
        return 0
    fi
    case "$PLATFORM" in
        macos)
            run open http://localhost:7424 >/dev/null 2>&1 || true
            ;;
        linux)
            if has xdg-open; then
                run xdg-open http://localhost:7424 >/dev/null 2>&1 || true
            fi
            ;;
    esac
}

# ───────── Final report ─────────
summary() {
    cat <<EOF

${C_GREEN}✔ Plinth is installed.${C_RESET}

  Dashboard : http://localhost:7424
  CLI       : ${C_BOLD}plinth${C_RESET} (try: plinth status, plinth logs, plinth demo)
  Install   : $PLINTH_HOME
  Logs      : $PLINTH_HOME/state/logs/
  Uninstall : plinth uninstall

${C_DIM}If 'plinth' is not in PATH, open a new shell or:
  source ~/.zshrc      # zsh
  source ~/.bashrc     # bash${C_RESET}

EOF
}

main() {
    parse_args "$@"
    banner
    detect_platform
    check_prereqs
    stop_existing
    fetch_repo
    ensure_state_dirs
    setup_venv
    install_cli
    install_autostart
    wait_for_services
    open_dashboard
    summary
}

main "$@"
