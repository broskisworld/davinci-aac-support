#!/usr/bin/env bash
#
# Packages the installer for download: a zip containing install.sh, its
# sibling daemon file, a double-clickable .desktop launcher, and the
# license/readme. Nothing in here is generated/embedded -- every file is
# exactly what's in the repo, so there's nothing to keep in sync.
#
# Usage: ./build-release-zip.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$SCRIPT_DIR/davinci-aac-support.zip"

FILES=(
    install.sh
    davinci_aac_support_watch.py
    davinci-aac-support.desktop
    README.md
    LICENSE
)

rm -f "$OUT"
cd "$SCRIPT_DIR"
zip -q "$OUT" "${FILES[@]}"
echo "Built: $OUT ($(du -h "$OUT" | cut -f1))"
unzip -l "$OUT"
