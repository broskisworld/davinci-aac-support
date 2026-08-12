#!/usr/bin/env bash
#
# Builds + runs the screenshot-capture container for all three distros
# (see docker/capture-screenshots.sh for what each one actually does),
# saving each distro's output under out/<distro>/. Debian is the
# canonical source: those two files also get copied into docs/images/,
# which is what the README actually references. The other two distros'
# screenshots exist purely as a visual-consistency check (they should
# render identically, since it's the same Python server + HTML/CSS
# regardless of which distro it's running on) -- CI uploads all three as
# artifacts but only commits Debian's into docs/.
#
# Usage: docker/capture-all-screenshots.sh [out-dir]  (default: out)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

OUT_ROOT="${1:-out}"
CANONICAL=debian

FAIL=0
for distro in debian fedora arch; do
    echo
    echo "########## $distro ##########"
    tag="davinci-aac-support-screenshot:$distro"
    out_dir="$OUT_ROOT/$distro"
    mkdir -p "$out_dir"

    if ! docker build -q -f "docker/Dockerfile.screenshot-$distro" -t "$tag" "$REPO_ROOT"; then
        echo "BUILD FAILED for $distro" >&2
        FAIL=1
        continue
    fi
    if ! docker run --rm -v "$out_dir:/out" "$tag"; then
        echo "CAPTURE FAILED for $distro" >&2
        FAIL=1
        continue
    fi
done

if [[ -f "$OUT_ROOT/$CANONICAL/installer-installing.png" && -f "$OUT_ROOT/$CANONICAL/installer-connected.png" ]]; then
    mkdir -p docs/images
    cp "$OUT_ROOT/$CANONICAL/installer-installing.png" docs/images/installer-installing.png
    cp "$OUT_ROOT/$CANONICAL/installer-connected.png" docs/images/installer-connected.png
    echo
    echo "Updated docs/images/installer-installing.png and installer-connected.png from $CANONICAL"
else
    echo "Canonical ($CANONICAL) screenshots missing -- not touching docs/images/" >&2
    FAIL=1
fi

exit $FAIL
