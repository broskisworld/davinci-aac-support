#!/usr/bin/env bash
#
# Basic tests for install.sh's argument parsing, help text, and the checks
# that should run before touching the system -- things testable without a
# real DaVinci Resolve install or systemd session (CI has neither).
#
# Usage: ./tests/test_install_cli.sh

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SH="$SCRIPT_DIR/../install.sh"

pass=0
fail=0

check() {
    local desc="$1" got="$2" want="$3"
    if [[ "$got" == "$want" ]]; then
        echo "  ok - $desc"
        pass=$((pass + 1))
    else
        echo "  FAIL - $desc"
        echo "    want: $want"
        echo "    got:  $got"
        fail=$((fail + 1))
    fi
}

check_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        echo "  ok - $desc"
        pass=$((pass + 1))
    else
        echo "  FAIL - $desc (expected to find: $needle)"
        fail=$((fail + 1))
    fi
}

echo "install.sh is syntactically valid bash"
if bash -n "$INSTALL_SH" 2>/tmp/syntax_err; then
    echo "  ok"
    pass=$((pass + 1))
else
    echo "  FAIL"
    cat /tmp/syntax_err
    fail=$((fail + 1))
fi
rm -f /tmp/syntax_err

echo "--help exits 0 and shows usage"
out=$("$INSTALL_SH" --help 2>&1)
code=$?
check "exit code" "$code" "0"
check_contains "mentions --status" "$out" "--status"
check_contains "mentions --uninstall" "$out" "--uninstall"

echo "-h is a synonym for --help"
out_h=$("$INSTALL_SH" -h 2>&1)
check "same output as --help" "$out_h" "$out"

echo "unknown argument exits non-zero with usage on stderr"
err=$("$INSTALL_SH" --bogus-flag 2>&1 1>/dev/null)
code=$?
if [[ "$code" -ne 0 ]]; then
    echo "  ok - non-zero exit"
    pass=$((pass + 1))
else
    echo "  FAIL - expected non-zero exit, got $code"
    fail=$((fail + 1))
fi
check_contains "mentions the bad flag" "$err" "--bogus-flag"

echo "--status without a prior install fails cleanly (no crash, no system changes)"
out=$(HOME=$(mktemp -d) "$INSTALL_SH" --status 2>&1)
code=$?
if [[ "$code" -ne 0 ]]; then
    echo "  ok - non-zero exit when nothing is installed"
    pass=$((pass + 1))
else
    echo "  FAIL - expected non-zero exit, got $code"
    fail=$((fail + 1))
fi
check_contains "says not installed" "$out" "Not installed"

echo
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
