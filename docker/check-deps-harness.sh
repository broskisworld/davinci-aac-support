#!/usr/bin/env bash
#
# check-deps-harness.sh -- runs inside each distro container to verify
# install.sh's actual package-manager detection and ffmpeg install logic,
# without running the rest of the installer (do_install() also calls
# check_resolve() and check_systemd(), which need a real /opt/resolve
# install and a live systemd --user session -- neither exists in a
# container, and that's out of scope here; see the README note in
# run-distro-tests.sh).
#
# Deliberately does NOT hand-copy check_deps()'s logic into this file.
# Instead it extracts the function verbatim out of the real install.sh at
# run time (sed between the `check_deps() {` and its closing `}`), so this
# harness can never silently drift out of sync with the script it's meant
# to be testing. What gets exercised is the literal code from install.sh,
# not a reimplementation of it.
#
# To make that extracted function runnable standalone, this stubs the
# handful of things check_deps() depends on from earlier in install.sh
# (the ui_*/warn helpers, and $GUI) and shims `sudo` to a plain passthrough
# -- containers already run as root, so sudo/pkexec's job of elevating
# privileges is moot, but the function still calls `sudo bash -c
# "$inner_cmd"` on the non-GUI path and we want that exact line to execute
# for real, not be skipped.
#
# Usage: check-deps-harness.sh <path-to-install.sh>

set -uo pipefail

INSTALL_SH="${1:?usage: check-deps-harness.sh <path-to-install.sh>}"
FAIL=0

section() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

section "bash -n syntax check"
if bash -n "$INSTALL_SH"; then
    echo "OK: install.sh is syntactically valid"
else
    echo "FAIL: syntax error in install.sh"
    FAIL=1
fi

section "raw package-manager detection (what command -v sees on this image)"
for pm in apt-get dnf pacman zypper; do
    if path=$(command -v "$pm" 2>/dev/null); then
        echo "  present: $pm -> $path"
    else
        echo "  absent:  $pm"
    fi
done

section "extracting check_deps() verbatim from install.sh"
FN=$(sed -n '/^check_deps() {/,/^}/p' "$INSTALL_SH")
if [[ -z "$FN" ]]; then
    echo "FAIL: could not find a 'check_deps() { ... }' block in install.sh -- extraction pattern is stale, harness needs updating"
    exit 1
fi
echo "$FN"
echo
echo "(extracted OK, $(echo "$FN" | wc -l) lines)"

# --- stubs for what check_deps() calls that live earlier in install.sh ---
# ui_fail must actually `exit` (not `return`) to faithfully match install.sh,
# where ui_fail() always calls exit 1 -- real check_deps() stops dead at the
# first failure rather than falling through to later ui_ok calls. Since
# check_deps is invoked below on the right-hand side of a pipe
# ("echo y | check_deps"), it already runs in its own subshell, so this
# exit only ends that subshell/pipeline stage, not the whole harness --
# the surrounding `if ... ; then ... else ...` below still sees a clean
# non-zero status and this script continues on to print diagnostics.
GUI=0
ui_step() { echo "[ui_step] $1"; }
ui_ok()   { echo "[ui_ok] $1"; }
ui_fail() { echo "[ui_fail] $1"; exit 1; }
warn()    { echo "[warn] $1"; }
# Containers run as root already; sudo just needs to exec its argv as-is
# (that's genuinely all sudo does once privileges are already sufficient).
sudo() { "$@"; }
export -f sudo

eval "$FN"

section "running the real check_deps() (auto-answering its Y/n prompt)"
if echo "y" | check_deps; then
    DEPS_OK=1
else
    DEPS_OK=0
    echo "check_deps() returned non-zero (see ui_fail above for why)"
fi

section "post-check_deps() verification"
if command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg:  $(ffmpeg -version 2>&1 | head -1)"
else
    echo "ffmpeg:  NOT FOUND"
    FAIL=1
fi
if command -v ffprobe >/dev/null 2>&1; then
    echo "ffprobe: $(ffprobe -version 2>&1 | head -1)"
else
    echo "ffprobe: NOT FOUND"
    FAIL=1
fi

section "systemctl binary presence (PID 1 / --user session is out of scope in a container)"
if command -v systemctl >/dev/null 2>&1; then
    echo "present: $(command -v systemctl) ($(systemctl --version 2>&1 | head -1))"
else
    echo "absent: systemctl is not present on this base image out of the box"
fi

if [[ $DEPS_OK -eq 0 ]]; then
    FAIL=1
fi

section "harness result"
if [[ $FAIL -eq 0 ]]; then
    echo "PASS"
else
    echo "FAIL (see above)"
fi
exit $FAIL
