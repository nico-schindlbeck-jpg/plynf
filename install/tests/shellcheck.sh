#!/bin/sh
# Run shellcheck against every shell script in install/. Exits 0 if all clean,
# 1 otherwise. Skipped (with a clear message) if shellcheck isn't installed —
# CI verifies on every PR.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$(cd "$HERE/.." && pwd)"

if ! command -v shellcheck >/dev/null 2>&1; then
    echo "shellcheck not installed — skipping. (apt-get install shellcheck / brew install shellcheck)"
    echo "CI runs it on every PR."
    exit 0
fi

rc=0
for f in \
    "$INSTALL_DIR/install.sh" \
    "$INSTALL_DIR/uninstall.sh" \
    "$INSTALL_DIR/plinth" \
    "$INSTALL_DIR/tests/test_install_sh.sh" \
    "$INSTALL_DIR/tests/shellcheck.sh"
do
    [ -f "$f" ] || continue
    echo "shellcheck $f"
    if ! shellcheck -s sh "$f"; then
        rc=1
    fi
done

if [ "$rc" -eq 0 ]; then
    echo "✔ shellcheck clean"
else
    echo "✘ shellcheck reported issues"
fi
exit "$rc"
