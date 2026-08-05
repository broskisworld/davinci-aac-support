#!/usr/bin/env bash
#
# DaVinci Resolve AAC audio auto-fix -- installer
# =================================================
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
# This script is self-contained -- copy this one file to another machine
# and run it, no other files from this repo required.
#
# Usage:
#   ./install-aac-fix.sh              Install (or update) the watcher
#   ./install-aac-fix.sh --status     Check whether it's installed & connected
#   ./install-aac-fix.sh --uninstall  Remove the service and installed files
#   ./install-aac-fix.sh -h           Show this help
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

BIN_DIR="$HOME/.local/bin"
SERVICE_DIR="$HOME/.config/systemd/user"
DAEMON_PATH="$BIN_DIR/resolve_aac_watch.py"
SERVICE_NAME="davinci-aac-fix.service"
SERVICE_PATH="$SERVICE_DIR/$SERVICE_NAME"
STATE_DIR="$HOME/.cache/resolve-aac-fix"
STATUS_FILE="$STATE_DIR/status.json"

# When run via the single-file .desktop package (script piped through
# "base64 -d | bash"), $0 is just the literal string "bash" -- not a real,
# re-runnable path. Detect that and fall back to raw systemctl/journalctl
# commands in any message that would otherwise tell the user to re-run "$0".
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
# .desktop launcher) and zenity available -- drive everything through native
# dialogs instead of terminal output.
GUI=0
if [[ ! -t 1 ]] && command -v zenity >/dev/null 2>&1; then
    GUI=1
fi

# || true on the fire-and-forget dialogs: zenity returns non-zero if the
# user closes them via the window's X button instead of the OK button (a
# completely normal thing to do), and under set -e an unguarded non-zero
# return here would silently kill the whole installer mid-flow. gui_question
# deliberately does NOT do this -- its exit code (0=Yes, 1=No) is the point.
gui_info()     { zenity --info --title="DaVinci Resolve AAC Fix" --text="$1" --width=420 2>/dev/null || true; }
gui_warn()     { zenity --warning --title="DaVinci Resolve AAC Fix" --text="$1" --width=420 2>/dev/null || true; }
gui_error()    { zenity --error --title="DaVinci Resolve AAC Fix" --text="$1" --width=420 2>/dev/null || true; }
gui_question() { zenity --question --title="DaVinci Resolve AAC Fix" --text="$1" --width=420 2>/dev/null; }

GUI_FIFO=""
GUI_PID=""
gui_progress_start() {
    GUI_FIFO=$(mktemp -u)
    mkfifo "$GUI_FIFO"
    # fd 9 is opened read-write so the parent's open(2) never blocks waiting
    # for a reader. zenity reads via its own plain redirect on the fifo path
    # (not a dup of fd 9), and the trailing "9<&-" closes the *subshell's*
    # inherited copy of fd 9 -- background jobs fork with all parent fds
    # intact, so without this, that inherited copy keeps the fifo's write
    # end alive even after gui_progress_stop closes fd 9 in the parent, and
    # zenity never sees EOF (confirmed by hanging in testing before this
    # explicit close was added).
    exec 9<> "$GUI_FIFO"
    zenity --progress --title="DaVinci Resolve AAC Fix" --text="$1" --pulsate --no-cancel --auto-close --width=420 < "$GUI_FIFO" 2>/dev/null 9<&- &
    GUI_PID=$!
}
gui_progress_update() {
    echo "# $1" >&9 2>/dev/null || true
}
gui_progress_stop() {
    if [[ -n "$GUI_PID" ]]; then
        exec 9>&- 2>/dev/null || true
        exec 9<&- 2>/dev/null || true
        wait "$GUI_PID" 2>/dev/null || true
        GUI_PID=""
    fi
    [[ -n "$GUI_FIFO" && -e "$GUI_FIFO" ]] && rm -f "$GUI_FIFO"
    GUI_FIFO=""
}

# ui_* wrap the step/ok/err/exit pattern so the same check functions drive
# either terminal output or the GUI progress dialog depending on $GUI.
ui_step() { if [[ $GUI -eq 1 ]]; then gui_progress_update "$1"; else step "$1"; fi; }
ui_ok()   { if [[ $GUI -eq 1 ]]; then gui_progress_update "$1"; else ok "$1"; fi; }
ui_fail() {
    if [[ $GUI -eq 1 ]]; then
        gui_progress_stop
        gui_error "$1"
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
        inner_cmd="pacman -S --noconfirm ffmpeg"
    elif command -v zypper >/dev/null 2>&1; then
        inner_cmd="zypper install -y ffmpeg"
    else
        ui_fail "ffmpeg is missing and no supported package manager was found (apt/dnf/pacman/zypper).\nInstall ffmpeg yourself, then re-run this script."
    fi

    if [[ $GUI -eq 1 ]]; then
        gui_progress_stop
        if ! gui_question "ffmpeg is required to fix AAC audio and wasn't found.\n\nInstall it now? You'll be asked for your password."; then
            ui_fail "ffmpeg is required. Cancelled."
        fi
        gui_progress_start "Installing ffmpeg (this can take a minute)..."
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
                extra="\nOn Fedora, ffmpeg isn't in the default repos -- you likely need RPM Fusion:\nhttps://rpmfusion.org/Configuration"
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

    cat > "$DAEMON_PATH" <<'PYDAEMON'
#!/usr/bin/env python3
"""Background watcher: auto-fixes AAC audio in DaVinci Resolve's open project.

Polls the current project's Media Pool. Any clip whose audio stream(s) are
AAC gets remuxed to PCM in place (video stream-copied, untouched) and the
same Media Pool item is refreshed via MediaPoolItem.ReplaceClip(), which
preserves its bin location and any timeline placements.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

RESOLVE_SCRIPT_API = "/opt/resolve/Developer/Scripting"
RESOLVE_SCRIPT_LIB = "/opt/resolve/libs/Fusion/fusionscript.so"

POLL_INTERVAL = float(os.environ.get("RESOLVE_AAC_WATCH_INTERVAL", "3"))
RECONNECT_INTERVAL = 5
STATUS_FILE = os.environ.get(
    "RESOLVE_AAC_STATUS_FILE", os.path.expanduser("~/.cache/resolve-aac-fix/status.json")
)
NOTIFY = shutil.which("notify-send")

_status_cache = {}  # uid -> "clean" | "fixed"
_fixed_count = 0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def notify(title, body):
    if NOTIFY:
        subprocess.run([NOTIFY, "-a", "Resolve AAC Fix", title, body], check=False)


def write_status(**fields):
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    current = {}
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE) as f:
                current = json.load(f)
        except Exception:
            current = {}
    current.update(fields)
    current["last_update"] = time.time()
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(current, f)
    os.replace(tmp, STATUS_FILE)


def _load_resolve_module():
    # Deferred rather than a top-level import so this module stays importable
    # (and its pure logic testable) on machines/CI runners without Resolve
    # installed -- fusionscript.so only needs to exist when we actually try
    # to connect, not merely to load this file.
    os.environ.setdefault("RESOLVE_SCRIPT_API", RESOLVE_SCRIPT_API)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", RESOLVE_SCRIPT_LIB)
    modules_path = os.path.join(RESOLVE_SCRIPT_API, "Modules")
    if modules_path not in sys.path:
        sys.path.append(modules_path)
    import DaVinciResolveScript as dvr
    return dvr


def connect_resolve():
    dvr = _load_resolve_module()
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        return None
    try:
        resolve.GetProductName()
    except Exception:
        return None
    return resolve


def has_aac_audio(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        log(f"  ffprobe failed on {path}: {e}")
        return False
    codecs = [c.strip() for c in out.stdout.splitlines() if c.strip()]
    return "aac" in codecs


def convert_in_place(path):
    # ffmpeg can't read and write the same path at once, so this converts to
    # a temp file in the SAME directory as the source (same filesystem, so
    # the final swap is an atomic rename, not a copy) and replaces the
    # original on success. No separate copy is left behind either way.
    directory = os.path.dirname(path) or "."
    ext = os.path.splitext(path)[1]
    try:
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=ext)
        os.close(fd)
    except OSError as e:
        log(f"  can't write alongside source, skipping (read-only mount?): {e}")
        return False

    log(f"  converting in place: {path}")
    t0 = time.time()
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", path, "-map", "0",
         "-c", "copy", "-c:a", "pcm_s16le", tmp_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log(f"  ffmpeg FAILED ({time.time()-t0:.0f}s): {result.stderr[-800:]}")
        os.remove(tmp_path)
        return False

    os.replace(tmp_path, path)
    log(f"  converted in place in {time.time()-t0:.0f}s")
    return True


def process_clip(clip):
    global _fixed_count
    uid = clip.GetUniqueId()
    status = _status_cache.get(uid)
    if status in ("clean", "fixed"):
        return

    path = clip.GetClipProperty("File Path")
    if not path or not os.path.isfile(path):
        return

    if not has_aac_audio(path):
        _status_cache[uid] = "clean"
        return

    name = clip.GetClipProperty("File Name") or os.path.basename(path)
    log(f"AAC audio detected: {name}")

    if not convert_in_place(path):
        notify("AAC fix failed", name)
        return

    # Same path in and out -- ReplaceClip still forces Resolve to re-read
    # the file's metadata (confirmed live: Audio Codec property flips from
    # "AAC" to "Linear PCM" after this call), which is what actually clears
    # the stale blank-audio state in the Media Pool.
    if clip.ReplaceClip(path):
        log(f"  refreshed in Media Pool: {name}")
        notify("AAC audio fixed", name)
        _status_cache[uid] = "fixed"
        _fixed_count += 1
        write_status(fixed_count=_fixed_count, last_fixed=name)
    else:
        log(f"  ReplaceClip FAILED for {name}")
        notify("AAC fix failed", f"ReplaceClip rejected {name}")


def walk_folder(folder, depth=0):
    if depth > 25:
        return
    for clip in folder.GetClipList():
        try:
            process_clip(clip)
        except Exception as e:
            log(f"  error processing clip: {e}")
    for sub in folder.GetSubFolderList():
        walk_folder(sub, depth + 1)


def main():
    global _fixed_count
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE) as f:
                _fixed_count = json.load(f).get("fixed_count", 0)
        except Exception:
            pass

    log("resolve-aac-watch starting")
    write_status(connected=False, fixed_count=_fixed_count)
    resolve = None
    current_project_name = None
    while True:
        if resolve is None:
            resolve = connect_resolve()
            if resolve is None:
                write_status(connected=False)
                time.sleep(RECONNECT_INTERVAL)
                continue
            product = resolve.GetProductName()
            version = resolve.GetVersionString()
            log(f"connected to {product} {version}")
            write_status(connected=True, product=product, version=version)

        try:
            pm = resolve.GetProjectManager()
            project = pm.GetCurrentProject()
            if project is None:
                write_status(connected=True, project=None)
                time.sleep(POLL_INTERVAL)
                continue

            name = project.GetName()
            if name != current_project_name:
                log(f"active project: {name}")
                current_project_name = name
            write_status(connected=True, project=name)

            root = project.GetMediaPool().GetRootFolder()
            walk_folder(root)
        except Exception as e:
            log(f"lost connection to Resolve ({e}); will retry")
            write_status(connected=False)
            resolve = None
            current_project_name = None

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
PYDAEMON
    chmod +x "$DAEMON_PATH"
    ui_ok "Watcher script installed"

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
    gui_info "The watcher can't connect until Resolve's external scripting API is turned on (it's off by default).\n\nIn Resolve:\nPreferences -> search box -> type \"scripting\"\n(or: Preferences -> System tab -> General)\n-> \"External scripting using\" -> Local -> Save\n\nClick OK once you've done that (with Resolve running and a project open)."

    gui_progress_start "Waiting for connection to DaVinci Resolve..."
    local waited=0
    local result=""
    while (( waited < 40 )); do
        result="$(check_connection_now)"
        [[ -n "$result" ]] && break
        gui_progress_update "Waiting for connection to DaVinci Resolve... (${waited}s)"
        sleep 1
        waited=$((waited + 1))
    done
    gui_progress_stop

    if [[ -n "$result" ]]; then
        local prod="${result%%|*}"
        local proj="${result##*|}"
        local msg="Connected to $prod"
        [[ -n "$proj" ]] && msg="$msg\nWatching project: $proj"
        gui_info "$msg"
    else
        gui_warn "Didn't see a connection after 40s.\n\nThe background service is still running and will keep retrying -- it'll pick up the connection whenever the setting is saved and a project is open.\n\nCheck any time by running from a terminal:\n$(self_status_hint)"
    fi
}

print_summary() {
    if [[ $GUI -eq 1 ]]; then
        gui_info "Installed.\n\nWatcher: $DAEMON_PATH\nService: $SERVICE_NAME (enabled, starts at login)\n\nFrom here on: just import AAC-audio clips into Resolve normally -- they'll be fixed in place (audio re-encoded to PCM directly in the original file, video untouched) within a few seconds, no action needed.\n\nCheck status any time from a terminal with:\n$(self_status_hint)"
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
        gui_progress_start "Starting installer..."
    else
        step "DaVinci Resolve AAC-fix -- install"
    fi
    [[ "$(uname -s)" == "Linux" ]] || ui_fail "This installer only supports Linux."
    check_resolve
    check_systemd
    check_deps
    write_files
    start_service
    gui_progress_stop
    confirm_connection
    print_summary
}

do_status() {
    step "DaVinci Resolve AAC-fix -- status"
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
    step "Uninstalling DaVinci Resolve AAC-fix"
    read -rp "  Remove the service and installed files? [y/N] " REPLY
    if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
        echo "  Cancelled."
        exit 0
    fi
    systemctl --user disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$SERVICE_PATH" "$DAEMON_PATH"
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
