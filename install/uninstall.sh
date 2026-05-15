#!/bin/sh
# Plinth uninstaller — reverses what install.sh did.
#
# Usage:
#   plinth uninstall                # interactive
#   sh install/uninstall.sh -y      # non-interactive, keep data dir
#   sh install/uninstall.sh -y --purge   # also delete ~/.plinth/state
#
# Aim: leave the system as it was before install. Idempotent — safe to run
# multiple times.
set -eu

PLINTH_HOME="${PLINTH_HOME:-$HOME/.plinth}"
PLINTH_BIN_DIR="${PLINTH_BIN_DIR:-$HOME/.local/bin}"
RC_MARKER_START="# >>> Plinth installer >>>"
RC_MARKER_END="# <<< Plinth installer <<<"

YES=0
PURGE=0

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)    YES=1 ;;
        --purge)     PURGE=1 ;;
        -h|--help)
            cat <<EOF
Plinth uninstaller

  -y, --yes      do not prompt
      --purge    also delete $PLINTH_HOME (including ~/.plinth/state)
  -h, --help     show this message
EOF
            exit 0
            ;;
        *)  printf 'unknown arg: %s\n' "$1" >&2; exit 1 ;;
    esac
    shift
done

info() { printf '%s\n' "$*"; }

OS="$(uname -s)"

info "Stopping Plinth services..."
case "$OS" in
    Darwin)
        plist="$HOME/Library/LaunchAgents/dev.plinth.services.plist"
        if [ -f "$plist" ]; then
            launchctl unload "$plist" >/dev/null 2>&1 || true
            rm -f "$plist"
        fi
        ;;
    Linux)
        if command -v systemctl >/dev/null 2>&1; then
            systemctl --user disable --now plinth.service >/dev/null 2>&1 || true
        fi
        rm -f "$HOME/.config/systemd/user/plinth.service"
        ;;
esac

info "Removing CLI..."
rm -f "$PLINTH_BIN_DIR/plinth"

info "Cleaning shell rc files..."
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile" "$HOME/.bash_profile"; do
    [ -f "$rc" ] || continue
    if grep -Fq "$RC_MARKER_START" "$rc" 2>/dev/null; then
        # Use awk to delete the marker block — portable across BSD + GNU.
        tmp="$(mktemp 2>/dev/null || mktemp -t plinth-rc)"
        awk -v s="$RC_MARKER_START" -v e="$RC_MARKER_END" '
            $0 == s { skip = 1; next }
            $0 == e { skip = 0; next }
            !skip   { print }
        ' "$rc" > "$tmp"
        mv "$tmp" "$rc"
        info "  cleaned $rc"
    fi
done

# Decide whether to wipe $PLINTH_HOME ------------------------------------------
should_purge=0
if [ "$PURGE" -eq 1 ]; then
    should_purge=1
elif [ "$YES" -eq 1 ]; then
    should_purge=0
elif [ -d "$PLINTH_HOME" ]; then
    printf 'Remove %s (workspace data, logs, venv, repo)? [y/N] ' "$PLINTH_HOME"
    read -r answer
    case "$answer" in
        [yY]|[yY][eE][sS]) should_purge=1 ;;
    esac
fi

if [ "$should_purge" -eq 1 ] && [ -d "$PLINTH_HOME" ]; then
    info "Removing $PLINTH_HOME ..."
    rm -rf "$PLINTH_HOME"
else
    if [ -d "$PLINTH_HOME" ]; then
        info "Keeping $PLINTH_HOME — remove manually with: rm -rf $PLINTH_HOME"
    fi
fi

cat <<EOF

✔ Plinth uninstalled.

If a shell rc file was modified, open a new shell (or re-source it) so
PATH no longer references $PLINTH_BIN_DIR/plinth.
EOF
