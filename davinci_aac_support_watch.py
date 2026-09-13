#!/usr/bin/env python3
"""Background watcher: auto-fixes AAC audio in DaVinci Resolve's open project.

Polls the current project's Media Pool. Any clip whose audio stream(s) are
AAC gets remuxed to PCM (video stream-copied, untouched) and the same Media
Pool item is refreshed via MediaPoolItem.ReplaceClip(), which preserves its
bin location and any timeline placements -- even when the file's path
changes (see conversion_mode in davinci_aac_support_config.py).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import davinci_aac_support_config as settings

RESOLVE_SCRIPT_API = "/opt/resolve/Developer/Scripting"
RESOLVE_SCRIPT_LIB = "/opt/resolve/libs/Fusion/fusionscript.so"

POLL_INTERVAL = float(os.environ.get("DAVINCI_AAC_SUPPORT_INTERVAL", "3"))
RECONNECT_INTERVAL = 5
STATUS_FILE = os.environ.get(
    "DAVINCI_AAC_SUPPORT_STATUS_FILE", os.path.expanduser("~/.cache/davinci-aac-support/status.json")
)
EVENTS_FILE = os.environ.get(
    "DAVINCI_AAC_SUPPORT_EVENTS_FILE", os.path.expanduser("~/.cache/davinci-aac-support/events.jsonl")
)
EVENTS_MAX_LINES = 200
NOTIFY = shutil.which("notify-send")

_status_cache = {}  # uid -> "clean" | "fixed"
_fixed_count = 0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def emit_event(kind, text):
    # Feeds the live monitor (davinci_aac_support_ui.py) via a plain
    # append-only JSONL file it tails -- kept short since it's just a
    # recent-activity feed, not an audit log.
    os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
    line = json.dumps({"type": "event", "kind": kind, "text": text, "time": time.time()})
    try:
        with open(EVENTS_FILE, "a") as f:
            f.write(line + "\n")
        with open(EVENTS_FILE) as f:
            lines = f.readlines()
        if len(lines) > EVENTS_MAX_LINES:
            with open(EVENTS_FILE, "w") as f:
                f.writelines(lines[-EVENTS_MAX_LINES:])
    except Exception:
        pass  # the live feed is a nice-to-have, never worth crashing the watcher over


def notify(title, body):
    if NOTIFY:
        subprocess.run([NOTIFY, "-a", "DaVinci AAC Support", title, body], check=False)


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


SAFE_MAP_ARGS = ["-map", "0:v?", "-map", "0:a?", "-c:v", "copy", "-c:a", "pcm_s16le"]

# "-map 0" (every stream) plus "-f mov": embeds anything else in the file
# (timecode, GPS/telemetry) too, but requires the QuickTime muxer -- some
# camera timecode tracks report no usable codec ID to ffmpeg's demuxer, and
# the standard MP4 muxer flatly refuses to write those at all, even with
# nothing else being re-encoded (confirmed live: same failure on a pure
# stream copy). QuickTime's own muxer will write them; the cost is the
# output file's container brand changes from mp4/isom to qt, even though
# the extension doesn't.
FULL_PRESERVE_ARGS = ["-map", "0", "-c", "copy", "-c:a", "pcm_s16le", "-f", "mov"]


def get_non_av_stream_types(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return []
    types = [t.strip() for t in out.stdout.splitlines() if t.strip()]
    return [t for t in types if t not in ("video", "audio")]


def _hashed_output_path(path, output_directory):
    # Deterministic from the source's absolute path: reprocessing the same
    # clip converges on the same output file (no pile-up across restarts),
    # while two different source files that happen to share a basename
    # never collide.
    stem, ext = os.path.splitext(os.path.basename(path))
    digest = hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:8]
    return os.path.join(output_directory, f"{stem}-{digest}{ext}")


def convert_clip(path):
    cfg = settings.load_config()
    separate_dir = cfg["conversion_mode"] == settings.MODE_SEPARATE_DIRECTORY

    if separate_dir:
        output_directory = cfg["output_directory"]
        try:
            os.makedirs(output_directory, exist_ok=True)
        except OSError as e:
            log(f"  can't create output directory {output_directory}: {e}")
            return None
        directory = output_directory
        final_path = _hashed_output_path(path, output_directory)
    else:
        # ffmpeg can't read and write the same path at once, so this
        # converts to a temp file in the SAME directory as the source (same
        # filesystem, so the final swap is an atomic rename, not a copy)
        # and replaces the original on success. No separate copy is left
        # behind -- the original file stops existing once this succeeds.
        directory = os.path.dirname(path) or "."
        final_path = path

    ext = os.path.splitext(path)[1]
    try:
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=ext)
        os.close(fd)
    except OSError as e:
        log(f"  can't write to {directory}, skipping (read-only mount?): {e}")
        return None

    non_av_streams = get_non_av_stream_types(path) if cfg["allow_container_change"] else []

    log(f"  converting: {path}" + ("" if final_path == path else f" -> {final_path}"))
    t0 = time.time()

    if non_av_streams:
        result = subprocess.run(["ffmpeg", "-y", "-i", path, *FULL_PRESERVE_ARGS, tmp_path],
                                 capture_output=True, text=True)
        if result.returncode != 0:
            log("  container-preserving conversion failed, falling back to video+audio only")
            result = subprocess.run(["ffmpeg", "-y", "-i", path, *SAFE_MAP_ARGS, tmp_path],
                                     capture_output=True, text=True)
    else:
        result = subprocess.run(["ffmpeg", "-y", "-i", path, *SAFE_MAP_ARGS, tmp_path],
                                 capture_output=True, text=True)

    if result.returncode != 0:
        log(f"  ffmpeg FAILED ({time.time()-t0:.0f}s): {result.stderr[-800:]}")
        os.remove(tmp_path)
        return None

    os.replace(tmp_path, final_path)
    log(f"  converted in {time.time()-t0:.0f}s")
    return final_path


def process_clip(clip):
    global _fixed_count
    uid = clip.GetUniqueId()
    status = _status_cache.get(uid)
    if status in ("clean", "fixed", "failed"):
        return

    path = clip.GetClipProperty("File Path")
    if not path or not os.path.isfile(path):
        return

    if not has_aac_audio(path):
        _status_cache[uid] = "clean"
        return

    name = clip.GetClipProperty("File Name") or os.path.basename(path)
    log(f"AAC audio detected: {name}")
    emit_event("detected", f"AAC audio detected: {name}")

    emit_event("converting", f"Converting: {name}")
    new_path = convert_clip(path)
    if not new_path:
        notify("AAC Support failed", name)
        emit_event("failed", f"Conversion failed: {name}")
        # Without this, a clip that fails once gets retried (and re-fires
        # this same notification) every poll interval forever -- confirmed
        # live as a real notification-spam bug, independent of whatever
        # caused the conversion itself to fail.
        _status_cache[uid] = "failed"
        return

    # In "in place" mode new_path == path, but ReplaceClip still forces
    # Resolve to re-read the file's metadata even then (confirmed live:
    # Audio Codec property flips from "AAC" to "Linear PCM" after this
    # call) -- that's what actually clears the stale blank-audio state.
    if clip.ReplaceClip(new_path):
        log(f"  refreshed in Media Pool: {name}")
        notify("AAC audio fixed", name)
        emit_event("fixed", f"Fixed: {name}")
        _status_cache[uid] = "fixed"
        _fixed_count += 1
        write_status(fixed_count=_fixed_count, last_fixed=name)
    else:
        log(f"  ReplaceClip FAILED for {name}")
        notify("AAC Support failed", f"ReplaceClip rejected {name}")
        emit_event("failed", f"Resolve rejected the refresh: {name}")
        _status_cache[uid] = "failed"


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

    log("davinci-aac-support watcher starting")
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
