#!/usr/bin/env bash
# Plinth — one-shot migration of /tmp/plinth-data to the OS-appropriate
# user data directory. Safe to run multiple times (idempotent).
#
# Triggered automatically by the Makefile on first `make serve` after
# v1.7.6 upgrade. Can also be run manually.
#
# Exit codes:
#   0 — nothing to migrate, OR migrated successfully, OR target already populated
#   1 — copy failed (disk full, permission)
#   2 — both source and target have data; user must decide

set -eu

LEGACY="/tmp/plinth-data"
SYS="$(uname -s)"

# Resolve OS-appropriate target.
case "$SYS" in
    Darwin)
        TARGET="${HOME}/Library/Application Support/Plinth"
        ;;
    Linux)
        TARGET="${XDG_DATA_HOME:-${HOME}/.local/share}/plinth"
        ;;
    MINGW*|MSYS*|CYGWIN*)
        TARGET="${APPDATA:-${HOME}/AppData/Roaming}/Plinth"
        ;;
    *)
        TARGET="${HOME}/.plinth/data"
        ;;
esac

# Allow override (used by tests).
TARGET="${PLINTH_DATA_DIR:-$TARGET}"

# Marker file written after a successful migration. Skip if present.
MARKER="${TARGET}/.migrated-from-tmp"

if [ -f "$MARKER" ]; then
    # Already migrated. No-op.
    exit 0
fi

# Legacy path doesn't exist or is empty → nothing to do.
if [ ! -d "$LEGACY" ] || [ -z "$(ls -A "$LEGACY" 2>/dev/null)" ]; then
    # First run, fresh install. Just ensure target exists.
    mkdir -p "$TARGET"
    touch "$MARKER"
    exit 0
fi

# Legacy has data. Check target.
if [ -d "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null)" ]; then
    # Both populated. Surface to user, do nothing dangerous.
    cat >&2 <<EOF
✘ Plinth data-dir migration: both locations contain data.
  Legacy: ${LEGACY}
  Target: ${TARGET}

  This usually means you ran 'make serve' on both an old and a new
  version, or you copied data manually.

  Please pick one:
    1. Keep target (recommended): rm -rf "${LEGACY}" and touch "${MARKER}"
    2. Restore legacy: rm -rf "${TARGET}" then re-run 'make serve'
    3. Manual merge: diff and copy the files you care about

  No data was touched.
EOF
    exit 2
fi

# Legacy has data, target empty/missing. Migrate.
mkdir -p "$TARGET"
echo "→ Plinth: migrating data from ${LEGACY} to ${TARGET}"

# Use rsync if available (preserves perms/times); fall back to cp -R.
if command -v rsync >/dev/null 2>&1; then
    rsync -a "${LEGACY}/" "${TARGET}/"
else
    cp -R "${LEGACY}/." "${TARGET}/"
fi

# Write marker so we don't redo this. Include source path + date.
{
    echo "Migrated from ${LEGACY}"
    echo "Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "Host: $(hostname)"
    echo ""
    echo "The legacy directory was NOT deleted automatically — verify the"
    echo "migration succeeded by running 'make serve' and inspecting your"
    echo "workspaces, then run:"
    echo "  rm -rf ${LEGACY}"
} > "$MARKER"

echo "✔ Migrated. Legacy preserved at ${LEGACY} until you remove it manually."
echo "  Verify with 'make serve' before deleting."
exit 0
