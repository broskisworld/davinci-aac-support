#!/usr/bin/env bash
#
# Runs inside each Dockerfile.screenshot-<distro> container. Drives the
# real install.sh (unmodified) through its GUI dashboard flow and
# screenshots it at two points, for documentation -- NOT a correctness
# test (docker/check-deps-harness.sh covers that). Since the dashboard is
# the same Python server + HTML/CSS regardless of which distro it's
# running on, these screenshots are visually identical across distros by
# construction; this runs on all three anyway (see
# docker/capture-all-screenshots.sh) so a broken render on any one distro
# still gets caught, even though only one distro's output is used in the
# committed docs.
#
# What's faked, and why: a real install needs an actual DaVinci Resolve
# GUI install and a live systemd --user session, neither of which exists
# in a bare container.
#   - /opt/resolve is stubbed with just enough of a directory structure
#     to pass check_resolve()'s two file/dir checks.
#   - systemctl is stubbed to unconditionally succeed -- install.sh's own
#     functional correctness (real systemd, real Resolve connection) is
#     already covered by every other test in this repo; this script only
#     cares about what the dashboard *looks like*.
#   - Since nothing here runs a real watcher daemon, confirm_connection()
#     would otherwise just time out waiting 40s for a status.json that
#     will never appear -- a fake one is written directly so the second
#     screenshot shows a realistic "connected" state instead of either an
#     empty wait or a 40-second CI delay.
#
# Usage: capture-screenshots.sh <output-dir>

set -euo pipefail
OUT="${1:?usage: capture-screenshots.sh <output-dir>}"
mkdir -p "$OUT"

# The chromium package installs its binary under a different name per
# distro (confirmed empirically: "chromium" on Debian/Arch, "chromium-browser"
# on Fedora) -- resolve whichever exists rather than hardcoding one.
CHROMIUM=""
for candidate in chromium chromium-browser; do
    if command -v "$candidate" >/dev/null 2>&1; then
        CHROMIUM="$candidate"
        break
    fi
done
[[ -n "$CHROMIUM" ]] || { echo "no chromium/chromium-browser binary found" >&2; exit 1; }

mkdir -p /opt/resolve/libs/Fusion
touch /opt/resolve/libs/Fusion/fusionscript.so

cat > /usr/local/bin/systemctl <<'EOF'
#!/bin/bash
exit 0
EOF
chmod +x /usr/local/bin/systemctl

cd /work

# Detached, stdin/stdout not a tty -> install.sh auto-selects GUI mode,
# same as a real double-click launch.
setsid ./install.sh </dev/null >/tmp/install-stdout.log 2>&1 &

STATE_DIR="$HOME/.cache/davinci-aac-support"
PORT_FILE="$STATE_DIR/ui-port.txt"
for _ in $(seq 1 100); do
    [[ -f "$PORT_FILE" ]] && break
    sleep 0.1
done
[[ -f "$PORT_FILE" ]] || { echo "dashboard never started (see /tmp/install-stdout.log)" >&2; cat /tmp/install-stdout.log >&2; exit 1; }
PORT="$(cat "$PORT_FILE")"

shoot() {
    local out="$1"
    "$CHROMIUM" --headless --disable-gpu --no-sandbox --disable-dev-shm-usage \
        --screenshot="$out" --window-size=760,900 --timeout=10000 \
        "http://127.0.0.1:$PORT/" >/dev/null 2>&1
}

sleep 0.6
shoot "$OUT/installer-installing.png"

# Give install.sh's own checks a moment to actually finish (files written,
# stubbed systemctl "started" it) before faking the connection -- otherwise
# the fake status.json could be overwritten by write_status(connected=false)
# calls still in flight from earlier in the script.
sleep 1.5
cat > "$STATE_DIR/status.json" <<JSON
{"connected": true, "product": "DaVinci Resolve Studio", "version": "18.6.6", "project": "Example Project", "fixed_count": 3, "last_fixed": "interview_b.mov", "last_update": $(date +%s)}
JSON

sleep 1.5
shoot "$OUT/installer-connected.png"

echo "captured:"
ls -la "$OUT"
