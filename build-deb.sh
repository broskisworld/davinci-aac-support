#!/usr/bin/env bash
#
# Builds a real .deb -- the "double-click, Software Install, password
# prompt, done" experience, for Debian/Ubuntu/Mint desktops specifically
# (unlike davinci-aac-support.zip, which is the portable cross-distro
# fallback). See deb/ for the package metadata (control, postinst, the
# app-menu .desktop entry).
#
# Design: this package only installs shared, read-only files to a fixed
# system path (/usr/share/davinci-aac-support/) plus an app-menu entry
# that launches install.sh from there. It deliberately does NOT try to
# set up the per-user systemd --user watcher service as part of the
# package install (postinst runs as root; reliably identifying "the real
# desktop user" from there, on a machine that might have several, isn't
# something to build on shaky assumptions) -- opening "DaVinci AAC
# Support" from the applications menu after installing runs the exact
# same install.sh dashboard flow the zip uses, which is what actually
# does the per-user setup. Since install.sh now lives at a fixed,
# absolute path, its existing sibling-file lookup (SCRIPT_DIR) finds the
# daemon/dashboard scripts here with zero code changes needed.
#
# Usage: ./build-deb.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VERSION="$(cat VERSION)"
DEBIAN_REVISION="1"
FULL_VERSION="${VERSION}-${DEBIAN_REVISION}"
OUT="davinci-aac-support_${FULL_VERSION}_all.deb"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PKG="$WORK/pkgroot"

mkdir -p \
    "$PKG/DEBIAN" \
    "$PKG/usr/share/davinci-aac-support" \
    "$PKG/usr/share/applications" \
    "$PKG/usr/share/doc/davinci-aac-support"

install -m 0755 install.sh "$PKG/usr/share/davinci-aac-support/install.sh"
install -m 0644 davinci_aac_support_watch.py "$PKG/usr/share/davinci-aac-support/davinci_aac_support_watch.py"
install -m 0644 davinci_aac_support_ui.py "$PKG/usr/share/davinci-aac-support/davinci_aac_support_ui.py"
install -m 0644 deb/davinci-aac-support-setup.desktop "$PKG/usr/share/applications/davinci-aac-support-setup.desktop"
install -m 0644 README.md "$PKG/usr/share/doc/davinci-aac-support/README.md"
install -m 0644 LICENSE "$PKG/usr/share/doc/davinci-aac-support/copyright"

sed "s/__VERSION__/$FULL_VERSION/" deb/control > "$PKG/DEBIAN/control"
install -m 0755 deb/postinst "$PKG/DEBIAN/postinst"

dpkg-deb --build --root-owner-group "$PKG" "$SCRIPT_DIR/$OUT"
echo "Built: $OUT"
dpkg-deb --info "$OUT"
dpkg-deb --contents "$OUT"
