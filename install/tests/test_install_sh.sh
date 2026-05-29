#!/bin/sh
# Plinth installer — self-test harness.
#
# Runs without bats so it works on every dev machine. Exits non-zero on
# failure; prints a green tick per test.
#
# Each test isolates its side-effects by pointing PLINTH_HOME +
# PLINTH_BIN_DIR at a temp directory and passing --skip-services
# --skip-autostart --skip-open so we don't touch the system or hit the
# network.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$(cd "$HERE/.." && pwd)"

INSTALL_SH="$INSTALL_DIR/install.sh"
UNINSTALL_SH="$INSTALL_DIR/uninstall.sh"
PLINTH_CLI="$INSTALL_DIR/plinth"
RUN_ALL_PY="$INSTALL_DIR/run_all.py"
LAUNCHD_TPL="$INSTALL_DIR/launchd/dev.plinth.services.plist.template"
SYSTEMD_TPL="$INSTALL_DIR/systemd/plinth.service.template"

PASS=0
FAIL=0

# Colour ----------------------------------------------------------------------
if [ -t 1 ]; then
    G="$(printf '\033[32m')"
    R="$(printf '\033[31m')"
    D="$(printf '\033[2m')"
    Z="$(printf '\033[0m')"
else
    G=""; R=""; D=""; Z=""
fi

ok()   { printf '  %s✔%s %s\n' "$G" "$Z" "$1"; PASS=$((PASS + 1)); }
fail() { printf '  %s✘%s %s\n     %s\n' "$R" "$Z" "$1" "$2"; FAIL=$((FAIL + 1)); }

# Test scaffolding ------------------------------------------------------------
group() {
    printf '\n%s%s%s\n' "$D" "$*" "$Z"
}

assert_file_exists() {
    if [ -f "$1" ]; then ok "exists: $1"; else fail "missing: $1" "expected file not found"; fi
}

assert_executable() {
    if [ -x "$1" ] || [ "$(head -c2 "$1" 2>/dev/null)" = "#!" ]; then
        ok "has shebang/exec: $1"
    else
        fail "not executable: $1" "expected #! shebang or +x bit"
    fi
}

# 1. File layout --------------------------------------------------------------
group "1. Files exist + are well-formed"
assert_file_exists "$INSTALL_SH"
assert_file_exists "$UNINSTALL_SH"
assert_file_exists "$PLINTH_CLI"
assert_file_exists "$RUN_ALL_PY"
assert_file_exists "$LAUNCHD_TPL"
assert_file_exists "$SYSTEMD_TPL"
assert_executable "$INSTALL_SH"
assert_executable "$UNINSTALL_SH"
assert_executable "$PLINTH_CLI"

# 2. Syntax checks ------------------------------------------------------------
group "2. Syntax (sh -n)"
for f in "$INSTALL_SH" "$UNINSTALL_SH" "$PLINTH_CLI"; do
    if sh -n "$f" 2>/tmp/plinth-syntax.err; then
        ok "sh -n $f"
    else
        fail "sh -n $f" "$(cat /tmp/plinth-syntax.err)"
    fi
done
if python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$RUN_ALL_PY" 2>/tmp/plinth-pysyntax.err; then
    ok "python3 syntax: $RUN_ALL_PY"
else
    fail "python3 syntax: $RUN_ALL_PY" "$(cat /tmp/plinth-pysyntax.err)"
fi

# 3. Optional shellcheck ------------------------------------------------------
group "3. shellcheck (optional)"
if command -v shellcheck >/dev/null 2>&1; then
    for f in "$INSTALL_SH" "$UNINSTALL_SH" "$PLINTH_CLI"; do
        if shellcheck -s sh "$f" >/tmp/plinth-shellcheck.out 2>&1; then
            ok "shellcheck $f"
        else
            fail "shellcheck $f" "$(cat /tmp/plinth-shellcheck.out)"
        fi
    done
else
    ok "shellcheck not installed locally — CI runs it"
fi

# 4. --help flag --------------------------------------------------------------
group "4. --help"
if "$INSTALL_SH" --help >/tmp/plinth-help.out 2>&1; then
    if grep -q -- "--verbose" /tmp/plinth-help.out && grep -q -- "PLINTH_HOME" /tmp/plinth-help.out; then
        ok "install.sh --help prints flag + env reference"
    else
        fail "install.sh --help" "expected --verbose and PLINTH_HOME mentions"
    fi
else
    fail "install.sh --help" "non-zero exit"
fi

# Run the CLI with no env: it should still print help.
if "$PLINTH_CLI" help >/tmp/plinth-cli-help.out 2>&1; then
    if grep -q "plinth status" /tmp/plinth-cli-help.out; then
        ok "plinth help prints usage"
    else
        fail "plinth help" "expected 'plinth status' in output"
    fi
else
    fail "plinth help" "non-zero exit"
fi

# 5. Unsupported-OS handling --------------------------------------------------
group "5. Unsupported OS guard"
TMP_OUT="$(mktemp)"
# We fake `uname` by injecting a shim earlier in PATH. The installer also
# requires git/curl/python3 — we don't need them to reach the OS check, but
# we have to short-circuit before the prereq scan. We use --dry-run +
# PLINTH_HOME to keep this safe.
SHIM_DIR="$(mktemp -d)"
cat >"$SHIM_DIR/uname" <<'EOF'
#!/bin/sh
echo "Windows_NT"
EOF
chmod +x "$SHIM_DIR/uname"
PATH="$SHIM_DIR:$PATH" \
    PLINTH_HOME="$(mktemp -d)" \
    sh "$INSTALL_SH" --skip-autostart --skip-services --skip-open --dry-run \
        >"$TMP_OUT" 2>&1 \
        && rc=0 || rc=$?
rm -rf "$SHIM_DIR"

if [ "$rc" -ne 0 ] && grep -qi "unsupported OS" "$TMP_OUT"; then
    ok "detects unsupported OS (Windows_NT) and exits non-zero"
else
    fail "unsupported OS guard" "rc=$rc, output:\n$(cat "$TMP_OUT")"
fi

# 6. Idempotency (dry-run twice) ----------------------------------------------
group "6. Idempotent dry-run"
SANDBOX="$(mktemp -d)"
TMP_OUT="$(mktemp)"

run_dry() {
    PLINTH_HOME="$SANDBOX/plinth" \
        PLINTH_BIN_DIR="$SANDBOX/bin" \
        sh "$INSTALL_SH" \
            --skip-autostart --skip-services --skip-open --dry-run --no-update \
        >"$TMP_OUT" 2>&1
}

if run_dry; then
    ok "first dry-run succeeds"
else
    fail "first dry-run" "$(cat "$TMP_OUT")"
fi

if run_dry; then
    ok "second dry-run succeeds (idempotent)"
else
    fail "second dry-run" "$(cat "$TMP_OUT")"
fi

rm -rf "$SANDBOX"

# 7. Templates are well-formed -----------------------------------------------
group "7. Templates"
if grep -q "<plist" "$LAUNCHD_TPL" && grep -q "__VENV__" "$LAUNCHD_TPL"; then
    ok "launchd template has plist root + placeholders"
else
    fail "launchd template" "missing <plist> or __VENV__"
fi

if grep -q "\[Service\]" "$SYSTEMD_TPL" && grep -q "__REPO__" "$SYSTEMD_TPL"; then
    ok "systemd template has [Service] + placeholders"
else
    fail "systemd template" "missing [Service] or __REPO__"
fi

# 8. Uninstaller --help -------------------------------------------------------
group "8. Uninstaller"
if "$UNINSTALL_SH" --help >/tmp/plinth-uninstall-help.out 2>&1; then
    if grep -q -- "--purge" /tmp/plinth-uninstall-help.out; then
        ok "uninstall.sh --help"
    else
        fail "uninstall.sh --help" "expected --purge in output"
    fi
else
    fail "uninstall.sh --help" "non-zero exit"
fi

# Summary --------------------------------------------------------------------
printf '\n%s%s tests passed, %s failed%s\n' "$D" "$PASS" "$FAIL" "$Z"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
