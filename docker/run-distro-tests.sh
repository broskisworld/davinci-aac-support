#!/usr/bin/env bash
#
# run-distro-tests.sh -- builds and runs the per-distro verification
# containers for install.sh's check_deps() package-manager detection and
# ffmpeg install logic (see check-deps-harness.sh for what's actually
# exercised, and the docstrings at the top of each Dockerfile for what
# each base image needed).
#
# What this deliberately does NOT test: the rest of install.sh
# (check_resolve, check_systemd's --user session, write_files,
# start_service, confirm_connection). Those need a real DaVinci Resolve
# GUI install at /opt/resolve and a live systemd --user session -- neither
# is available in a bare container, and forcing a full systemd PID 1 into
# one is out of scope (see check-deps-harness.sh's systemctl section,
# which only checks binary presence).
#
# Usage:
#   docker/run-distro-tests.sh              # build + run all three
#   docker/run-distro-tests.sh fedora arch   # just these
#
# Exit code is non-zero if any distro's harness fails -- meant to be
# wired into CI directly once this is copied into a workflow.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

ALL_DISTROS=(fedora arch debian)
if [[ $# -gt 0 ]]; then
    DISTROS=("$@")
else
    DISTROS=("${ALL_DISTROS[@]}")
fi

declare -A RESULT

for distro in "${DISTROS[@]}"; do
    dockerfile="docker/Dockerfile.$distro"
    if [[ ! -f "$dockerfile" ]]; then
        echo "skip: no $dockerfile" >&2
        RESULT[$distro]="NO DOCKERFILE"
        continue
    fi

    printf '\n\033[1;35m########## %s ##########\033[0m\n' "$distro"

    tag="davinci-aac-support-test:$distro"
    echo "-- building $tag from $dockerfile (context: $REPO_ROOT) --"
    if ! docker build -q -f "$dockerfile" -t "$tag" "$REPO_ROOT" >/tmp/docker-build-$distro.log 2>&1; then
        echo "BUILD FAILED for $distro:"
        cat /tmp/docker-build-$distro.log
        RESULT[$distro]="BUILD FAILED"
        continue
    fi

    echo "-- running $tag --"
    if docker run --rm "$tag"; then
        RESULT[$distro]="PASS"
    else
        RESULT[$distro]="FAIL"
    fi
done

printf '\n\033[1m========== summary ==========\033[0m\n'
overall=0
for distro in "${DISTROS[@]}"; do
    r="${RESULT[$distro]:-(not run)}"
    printf '  %-10s %s\n' "$distro" "$r"
    [[ "$r" == "PASS" ]] || overall=1
done

exit $overall
