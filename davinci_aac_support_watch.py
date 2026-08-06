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

POLL_INTERVAL = float(os.environ.get("DAVINCI_AAC_SUPPORT_INTERVAL", "3"))
RECONNECT_INTERVAL = 5
STATUS_FILE = os.environ.get(
    "DAVINCI_AAC_SUPPORT_STATUS_FILE", os.path.expanduser("~/.cache/davinci-aac-support/status.json")
)
NOTIFY = shutil.which("notify-send")

_status_cache = {}  # uid -> "clean" | "fixed"
_fixed_count = 0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


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
        notify("AAC Support failed", name)
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
        notify("AAC Support failed", f"ReplaceClip rejected {name}")


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
