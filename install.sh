#!/usr/bin/env bash
#
# DaVinci AAC Support -- installer
# =================================
#
# Resolve on Linux has no licensed AAC decoder (true for both Free and
# Studio -- it's a licensing gap, not a missing system codec). Any imported
# clip with AAC audio (most camera/phone .mov/.mp4 files) shows up in the
# Media Pool with a silent, blank audio track.
#
# This installs a small background watcher that fixes it automatically:
# it connects to a running Resolve instance over Resolve's own scripting
# API, polls the open project's Media Pool, and for any clip whose audio is
# AAC, remuxes just the audio to PCM (video is stream-copied -- zero
# quality loss, no re-encode) and swaps it into the *same* Media Pool item
# via ReplaceClip(). That preserves the clip's bin location and any
# timeline placements -- nothing needs to be deleted or re-imported by
# hand. It runs as a systemd --user service, so it's always on whenever
# you're logged in, independent of whether Resolve is open yet.
#
# Run this from the extracted zip (it picks up davinci_aac_support_watch.py
# sitting next to it) or standalone, e.g. via:
#   curl -fsSL https://raw.githubusercontent.com/broskisworld/davinci-aac-support/main/install.sh | bash
# (standalone, it fetches the daemon file straight from GitHub instead).
#
# Usage:
#   ./install.sh              Install (or update) the watcher
#   ./install.sh --status     Check whether it's installed & connected
#   ./install.sh --uninstall  Remove the service and installed files
#   ./install.sh -h           Show this help
#
# Requirements: Linux, systemd --user, DaVinci Resolve installed at
# /opt/resolve (Resolve's standard Linux location), ffmpeg/ffprobe
# (installed automatically if missing and a supported package manager --
# apt/dnf/pacman/zypper -- is found).
#
# One step this script cannot do for you: Resolve's external scripting API
# is off by default and can only be toggled from its own GUI. The installer
# walks you through exactly where to find it and confirms the connection
# live before finishing.

set -euo pipefail

usage() {
    sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" | sed '$d' | sed -E 's/^#//; s/^ //'
}

ACTION="install"
case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --status) ACTION="status" ;;
    --uninstall) ACTION="uninstall" ;;
    "") ACTION="install" ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GITHUB_RAW_BASE="https://raw.githubusercontent.com/broskisworld/davinci-aac-support/main"

BIN_DIR="$HOME/.local/bin"
SERVICE_DIR="$HOME/.config/systemd/user"
DAEMON_PATH="$BIN_DIR/davinci_aac_support_watch.py"
UI_PATH="$BIN_DIR/davinci_aac_support_ui.py"
MONITOR_PATH="$BIN_DIR/davinci-aac-support-monitor"
SERVICE_NAME="davinci-aac-support.service"
SERVICE_PATH="$SERVICE_DIR/$SERVICE_NAME"
STATE_DIR="$HOME/.cache/davinci-aac-support"
STATUS_FILE="$STATE_DIR/status.json"
INSTALL_LOG="$STATE_DIR/install-log.jsonl"
PORT_FILE="$STATE_DIR/ui-port.txt"

# When run via "curl | bash", $0 is just the literal string "bash" -- not a
# real, re-runnable path. Detect that and fall back to raw systemctl/
# journalctl commands in any message that would otherwise tell the user to
# re-run "$0".
if [[ -f "${0:-}" ]]; then
    SELF="$0"
else
    SELF=""
fi
self_status_hint() {
    if [[ -n "$SELF" ]]; then
        echo "$SELF --status"
    else
        echo "journalctl --user -u $SERVICE_NAME -f  (or: systemctl --user status $SERVICE_NAME)"
    fi
}
self_uninstall_hint() {
    if [[ -n "$SELF" ]]; then
        echo "$SELF --uninstall"
    else
        echo "systemctl --user disable --now $SERVICE_NAME && rm -f $DAEMON_PATH $SERVICE_PATH && systemctl --user daemon-reload"
    fi
}

if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_CYAN=$'\033[36m'
else
    C_RESET=""; C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_CYAN=""
fi
ok()   { printf "  %s\xe2\x9c\x94%s %s\n" "$C_GREEN" "$C_RESET" "$1"; }
warn() { printf "  %s\xe2\x9a\xa0%s %s\n" "$C_YELLOW" "$C_RESET" "$1"; }
err()  { printf "  %s\xe2\x9c\x98%s %s\n" "$C_RED" "$C_RESET" "$1" >&2; }
step() { printf "\n%s%s%s\n" "${C_BOLD}${C_CYAN}" "$1" "$C_RESET"; }

# GUI mode: launched with no controlling terminal (e.g. double-clicked via a
# .desktop launcher) -- drive everything through a local web dashboard
# (davinci_aac_support_ui.py) opened in the default browser, instead of
# terminal output. No GUI-toolkit dependency (zenity, kdialog, ...) needed
# for this at all -- a browser doesn't care which desktop environment or
# toolkit is installed, which is exactly what makes this portable.
GUI=0
if [[ ! -t 1 ]]; then
    GUI=1
fi

UI_SERVER_PID=""

start_ui_server() {
    mkdir -p "$STATE_DIR"
    : > "$INSTALL_LOG"
    rm -f "$PORT_FILE"

    local ui_source="$SCRIPT_DIR/davinci_aac_support_ui.py"
    local ui_script="$ui_source"
    if [[ ! -f "$ui_source" ]]; then
        ui_script="$STATE_DIR/davinci_aac_support_ui.py"
        curl -fsSL "$GITHUB_RAW_BASE/davinci_aac_support_ui.py" -o "$ui_script" || return 1
    fi

    python3 "$ui_script" --mode install >/dev/null 2>&1 &
    UI_SERVER_PID=$!

    local waited=0
    while [[ ! -f "$PORT_FILE" ]] && (( waited < 50 )); do
        sleep 0.1
        waited=$((waited + 1))
    done
}

# Appends one structured line to the install log that the dashboard page
# streams live via SSE. Shelling out to python per line (rather than
# hand-building JSON in bash) avoids fragile manual escaping of the
# messages below, several of which contain quotes and newlines.
gui_log() {  # $1=type $2=text $3=id(optional, for "ask")
    python3 - "$INSTALL_LOG" "$1" "$2" "${3:-}" <<'PYEOF'
import json, sys
log_path, kind, text, extra_id = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
obj = {"type": kind, "text": text}
if extra_id:
    obj["id"] = extra_id
with open(log_path, "a") as f:
    f.write(json.dumps(obj) + "\n")
PYEOF
}
gui_log_step() { gui_log step "$1"; }
gui_log_ok()   { gui_log ok "$1"; }
gui_log_fail() { gui_log fail "$1"; }

# $1=id $2=text -- posts a yes/no prompt to the page and blocks (up to 5m)
# until the user clicks one of the buttons there. 0=yes, 1=no/timeout.
gui_ask() {
    local id="$1" text="$2" answer_file="$STATE_DIR/answer-$1.txt"
    rm -f "$answer_file"
    gui_log ask "$text" "$id"
    local waited=0
    while [[ ! -f "$answer_file" ]] && (( waited < 300 )); do
        sleep 1
        waited=$((waited + 1))
    done
    [[ -f "$answer_file" ]] || return 1
    local answer
    answer="$(cat "$answer_file")"
    rm -f "$answer_file"
    [[ "$answer" == "yes" ]]
}

# ui_* wrap the step/ok/err/exit pattern so the same check functions drive
# either terminal output or the dashboard log depending on $GUI.
ui_step() { if [[ $GUI -eq 1 ]]; then gui_log_step "$1"; else step "$1"; fi; }
ui_ok()   { if [[ $GUI -eq 1 ]]; then gui_log_ok "$1"; else ok "$1"; fi; }
ui_fail() {
    if [[ $GUI -eq 1 ]]; then
        gui_log_fail "$1"
    else
        err "$1"
    fi
    exit 1
}

check_resolve() {
    ui_step "Checking for DaVinci Resolve..."
    if [[ ! -d /opt/resolve ]]; then
        ui_fail "DaVinci Resolve not found at /opt/resolve.\nThis installer only supports Resolve's standard Linux install location."
    fi
    if [[ ! -f /opt/resolve/libs/Fusion/fusionscript.so ]]; then
        ui_fail "Found /opt/resolve but not its scripting library (fusionscript.so).\nThe install may be incomplete or a version this script wasn't tested against."
    fi
    ui_ok "Found DaVinci Resolve at /opt/resolve"
}

check_systemd() {
    ui_step "Checking systemd..."
    if ! command -v systemctl >/dev/null 2>&1; then
        ui_fail "systemctl not found -- this installer needs a systemd user session."
    fi
    if ! systemctl --user status >/dev/null 2>&1; then
        ui_fail "systemd --user session isn't available. Are you in a graphical/login session?"
    fi
    ui_ok "systemd user session available"
}

check_deps() {
    ui_step "Checking dependencies..."
    if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
        ui_ok "ffmpeg / ffprobe found"
        return
    fi

    local inner_cmd=""
    if command -v apt-get >/dev/null 2>&1; then
        inner_cmd="apt-get update && apt-get install -y ffmpeg"
    elif command -v dnf >/dev/null 2>&1; then
        inner_cmd="dnf install -y ffmpeg"
    elif command -v pacman >/dev/null 2>&1; then
        inner_cmd="pacman -Sy --noconfirm ffmpeg"
    elif command -v zypper >/dev/null 2>&1; then
        inner_cmd="zypper install -y ffmpeg"
    else
        ui_fail "ffmpeg is missing and no supported package manager was found (apt/dnf/pacman/zypper).\nInstall ffmpeg yourself, then re-run this script."
    fi

    if [[ $GUI -eq 1 ]]; then
        if ! gui_ask "ffmpeg-install" "ffmpeg is required to fix AAC audio and wasn't found. Install it now? You'll be asked for your password."; then
            ui_fail "ffmpeg is required. Cancelled."
        fi
        gui_log_step "Installing ffmpeg (this can take a minute)..."
        if ! pkexec bash -c "$inner_cmd"; then
            ui_fail "ffmpeg install failed or the password prompt was cancelled."
        fi
    else
        warn "ffmpeg not found -- attempting to install it"
        echo "  Will run: sudo bash -c \"$inner_cmd\""
        read -rp "  Proceed? [Y/n] " REPLY
        if [[ "$REPLY" =~ ^[Nn]$ ]]; then
            ui_fail "ffmpeg is required. Aborting."
        fi
        if ! sudo bash -c "$inner_cmd"; then
            local extra=""
            if command -v dnf >/dev/null 2>&1; then
                extra="\nOn Fedora, ffmpeg installs from the default repos on current releases -- if this failed, check your network/mirror first. On older releases it may still need RPM Fusion:\nhttps://rpmfusion.org/Configuration"
            fi
            ui_fail "Package install failed.$extra"
        fi
    fi

    command -v ffmpeg >/dev/null 2>&1 || ui_fail "ffmpeg still not found after install attempt."
    ui_ok "ffmpeg installed"
}

write_files() {
    ui_step "Installing files..."
    mkdir -p "$BIN_DIR" "$SERVICE_DIR" "$STATE_DIR"

    # The daemon is a normal sibling file in this repo/zip, not embedded --
    # copy it if it's sitting next to this script (the common case: cloned
    # repo or extracted zip), otherwise fetch it fresh (the "curl | bash"
    # case, where there is no sibling file to find).
    local daemon_source="$SCRIPT_DIR/davinci_aac_support_watch.py"
    if [[ -f "$daemon_source" ]]; then
        cp "$daemon_source" "$DAEMON_PATH"
    else
        if ! curl -fsSL "$GITHUB_RAW_BASE/davinci_aac_support_watch.py" -o "$DAEMON_PATH"; then
            ui_fail "Couldn't fetch davinci_aac_support_watch.py from GitHub and no local copy was found next to install.sh."
        fi
    fi
    chmod +x "$DAEMON_PATH"
    ui_ok "Watcher script installed"

    local ui_source="$SCRIPT_DIR/davinci_aac_support_ui.py"
    if [[ -f "$ui_source" ]]; then
        cp "$ui_source" "$UI_PATH"
    else
        if ! curl -fsSL "$GITHUB_RAW_BASE/davinci_aac_support_ui.py" -o "$UI_PATH"; then
            ui_fail "Couldn't fetch davinci_aac_support_ui.py from GitHub and no local copy was found next to install.sh."
        fi
    fi
    chmod +x "$UI_PATH"
    ui_ok "Dashboard installed"

    cat > "$MONITOR_PATH" <<MONEOF
#!/usr/bin/env bash
exec python3 "$UI_PATH" --mode monitor
MONEOF
    chmod +x "$MONITOR_PATH"
    ui_ok "Live monitor installed: davinci-aac-support-monitor"

    cat > "$SERVICE_PATH" <<UNITEOF
[Unit]
Description=Auto-fix AAC audio in DaVinci Resolve Media Pool
After=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 $DAEMON_PATH
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNITEOF
    ui_ok "systemd unit installed"
}

start_service() {
    ui_step "Starting service..."
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user restart "$SERVICE_NAME"
    sleep 1
    if systemctl --user is-active --quiet "$SERVICE_NAME"; then
        ui_ok "Service is running (enabled at login)"
    else
        local log
        log="$(journalctl --user -u "$SERVICE_NAME" --no-pager -n 20)"
        ui_fail "Service failed to start.\n\nRecent log:\n$log"
    fi
}

check_connection_now() {
    # Prints "product version|project" on stdout if freshly connected, nothing otherwise.
    [[ -f "$STATUS_FILE" ]] || return 0
    python3 - "$STATUS_FILE" <<'PYCHECK'
import json, sys, time
try:
    with open(sys.argv[1]) as f:
        s = json.load(f)
    if s.get("connected") and time.time() - s.get("last_update", 0) < 10:
        print(f"{s.get('product','Resolve')} {s.get('version','')}|{s.get('project') or ''}")
except Exception:
    pass
PYCHECK
}

confirm_connection() {
    if [[ $GUI -eq 1 ]]; then
        confirm_connection_gui
    else
        confirm_connection_cli
    fi
}

confirm_connection_cli() {
    step "One manual step in DaVinci Resolve"
    cat <<'EOF'

  The watcher can't connect until Resolve's external scripting API is
  turned on (it's off by default). In Resolve:

    Preferences -> search box (top-left) -> type "scripting"
    (or, if there's no search box: Preferences -> System tab -> General)
    -> "External scripting using" -> Local -> Save

EOF
    if [[ -t 0 ]]; then
        read -rp "  Press Enter once you've done that (with Resolve running, a project open)... " _
    else
        echo "  (non-interactive shell -- skipping the prompt, polling for up to 40s)"
    fi

    printf "  Waiting for connection "
    local spin_chars='|/-\'
    local i=0
    local waited=0
    local result=""
    while (( waited < 40 )); do
        result="$(check_connection_now)"
        [[ -n "$result" ]] && break
        printf "\r  Waiting for connection %s" "${spin_chars:$((i % 4)):1}"
        i=$((i + 1))
        sleep 1
        waited=$((waited + 1))
    done

    if [[ -n "$result" ]]; then
        echo
        local prod="${result%%|*}"
        local proj="${result##*|}"
        ok "Connected to $prod"
        [[ -n "$proj" ]] && ok "Watching project: $proj"
    else
        echo
        warn "Didn't see a connection after 40s."
        warn "The service is still running and will keep retrying in the background --"
        warn "it'll pick up the connection whenever the setting is saved and a project"
        warn "is open. Check any time with: $(self_status_hint)"
    fi
}

confirm_connection_gui() {
    gui_log_step 'One manual step in DaVinci Resolve: Preferences -> search "scripting" -> External scripting using -> Local -> Save. The status above updates live, so you will see it connect here as soon as that is saved.'

    local waited=0
    local result=""
    while (( waited < 40 )); do
        result="$(check_connection_now)"
        [[ -n "$result" ]] && break
        sleep 1
        waited=$((waited + 1))
    done

    if [[ -n "$result" ]]; then
        local prod="${result%%|*}"
        gui_log_ok "Connected to $prod"
    else
        gui_log_step "Still waiting -- this page keeps checking, so it'll update the moment the setting is saved. No need to keep this window open if you'd rather come back to it later; the watcher itself runs in the background regardless."
    fi
}

print_summary() {
    if [[ $GUI -eq 1 ]]; then
        gui_log_ok "Installed. Import AAC-audio clips into Resolve normally from here on -- they'll be fixed in place within a few seconds, no action needed. Run davinci-aac-support-monitor any time to watch it work."
        return
    fi
    step "Done"
    cat <<EOF
  Installed:
    Watcher script:     $DAEMON_PATH
    systemd service:    $SERVICE_PATH  (enabled, starts at login)

  Useful commands:
    $(self_status_hint)
    journalctl --user -u $SERVICE_NAME -f      Live logs
    systemctl --user restart $SERVICE_NAME     Restart the watcher
    $(self_uninstall_hint)

  From here on: just import AAC-audio clips into Resolve normally -- the
  watcher fixes them in place (audio re-encoded to PCM directly in the
  original file, video untouched) within a few seconds, no action needed.
EOF
}

do_install() {
    if [[ $GUI -eq 1 ]]; then
        start_ui_server
        if [[ ! -f "$PORT_FILE" ]]; then
            # No dashboard to report through and no terminal attached either
            # -- notify-send is the one thing left that has a real chance
            # of the user actually seeing this.
            command -v notify-send >/dev/null 2>&1 && \
                notify-send -a "DaVinci AAC Support" "Install failed" "The installer dashboard didn't start. Try running install.sh from a terminal instead to see what went wrong."
            exit 1
        fi
        # The dashboard server stays running after this script exits (its
        # own 30-minute idle timeout cleans it up) so the page -- status,
        # restart/uninstall buttons -- keeps working without a terminal
        # attached to anything.
    else
        step "DaVinci AAC Support -- install"
    fi
    [[ "$(uname -s)" == "Linux" ]] || ui_fail "This installer only supports Linux."
    check_resolve
    check_systemd
    check_deps
    write_files
    start_service
    confirm_connection
    print_summary
}

do_status() {
    step "DaVinci AAC Support -- status"
    if [[ ! -f "$SERVICE_PATH" ]]; then
        if [[ -n "$SELF" ]]; then
            warn "Not installed. Run: $SELF"
        else
            warn "Not installed. Re-run the installer (with no arguments) first."
        fi
        exit 1
    fi

    if systemctl --user is-active --quiet "$SERVICE_NAME"; then
        ok "Service is running"
    else
        err "Service is not running"
        echo "  Recent log:"
        journalctl --user -u "$SERVICE_NAME" --no-pager -n 10 | sed 's/^/    /'
        exit 1
    fi

    if [[ -f "$STATUS_FILE" ]]; then
        python3 - "$STATUS_FILE" <<'PYSTATUS'
import json, sys, time
with open(sys.argv[1]) as f:
    s = json.load(f)
age = time.time() - s.get("last_update", 0)
print(f"  Connected:   {'yes' if s.get('connected') else 'no'}")
if s.get("product"):
    print(f"  Resolve:     {s['product']} {s.get('version','')}")
if s.get("project"):
    print(f"  Project:     {s['project']}")
print(f"  Clips fixed: {s.get('fixed_count', 0)}")
if s.get("last_fixed"):
    print(f"  Last fixed:  {s['last_fixed']}")
print(f"  Last update: {age:.0f}s ago")
PYSTATUS
    else
        warn "No status data yet -- give it a few seconds after Resolve opens a project."
    fi
    echo
    echo "  Live logs: journalctl --user -u $SERVICE_NAME -f"
}

do_uninstall() {
    step "Uninstalling DaVinci AAC Support"
    read -rp "  Remove the service and installed files? [y/N] " REPLY
    if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
        echo "  Cancelled."
        exit 0
    fi
    systemctl --user disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$SERVICE_PATH" "$DAEMON_PATH" "$UI_PATH" "$MONITOR_PATH"
    rm -rf "$STATE_DIR"
    systemctl --user daemon-reload
    ok "Service removed."
    echo
    echo "  Note: any clips it already fixed keep their PCM audio -- fixing is"
    echo "  in-place, so there's no separate cache to clean up and nothing to undo."
}

case "$ACTION" in
    install) do_install ;;
    status) do_status ;;
    uninstall) do_uninstall ;;
esac
