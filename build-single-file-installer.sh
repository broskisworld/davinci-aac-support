#!/usr/bin/env bash
#
# Builds DaVinci-Resolve-AAC-Fix-Installer.desktop from install-aac-fix.sh --
# a single, self-contained file someone can download and double-click, no
# terminal or sibling files required.
#
# How: install-aac-fix.sh is base64-encoded and embedded directly in the
# .desktop file's Exec= line ("echo <blob> | base64 -d | bash"). This is
# deliberate, not incidental -- a .desktop's Exec= field code (%k, meant to
# expand to the launcher's own location so a *separate* sibling script could
# be found next to it) was tested against this machine's GIO/Nautilus stack
# and confirmed unsupported (comes back empty), so a two-file "launcher +
# script sitting next to it" bundle can't reliably find its own script
# depending on where it's extracted. Embedding sidesteps that entirely.
#
# install-aac-fix.sh remains the single source of truth -- re-run this
# script any time it changes to regenerate the bundle; never hand-edit the
# generated .desktop file.
#
# Usage: ./build-single-file-installer.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/install.sh"
OUT="$SCRIPT_DIR/DaVinci-Resolve-AAC-Fix-Installer.desktop"

[[ -f "$SRC" ]] || { echo "Not found: $SRC" >&2; exit 1; }

B64=$(base64 -w0 "$SRC")

cat > "$OUT" <<DESKEOF
[Desktop Entry]
Type=Application
Name=DaVinci Resolve AAC Audio Fix - Installer
Comment=Installs a background fix for DaVinci Resolve's silent AAC audio import bug
Icon=multimedia-volume-control
Terminal=false
Categories=AudioVideo;
Exec=bash -c "echo $B64 | base64 -d | bash"
DESKEOF

chmod +x "$OUT"
echo "Built: $OUT ($(wc -c < "$OUT") bytes)"
