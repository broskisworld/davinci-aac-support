"""Persistent, user-editable settings shared by the watcher and dashboard.

Plain JSON file, read fresh each time (no in-memory caching) so a change
made through the dashboard applies on the watcher's next poll, without a
service restart.
"""
import json
import os

CONFIG_FILE = os.environ.get(
    "DAVINCI_AAC_SUPPORT_CONFIG_FILE", os.path.expanduser("~/.config/davinci-aac-support/config.json")
)

MODE_IN_PLACE = "in_place"
MODE_SEPARATE_DIRECTORY = "separate_directory"

DEFAULTS = {
    # "in_place": overwrite the original file at its existing path.
    # "separate_directory": write the fixed copy elsewhere; the original is
    # never touched, so it's always the ultimate fallback/backup.
    "conversion_mode": MODE_IN_PLACE,
    "output_directory": os.path.expanduser("~/DaVinciAacSupportFixed"),
    # Some camera files carry a non-audio/video stream (timecode, GPS/
    # telemetry) that ffmpeg's standard MP4 muxer can't write back out.
    # Embedding it anyway requires switching to the QuickTime muxer, which
    # changes the file's container brand (mp4/isom -> qt) even though the
    # extension stays the same. Off by default since that's a real change
    # to what the file technically is, not just an internal flag.
    "allow_container_change": False,
}


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    merged = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in data:
            merged[key] = data[key]
    return merged


def save_config(updates):
    config = load_config()
    config.update({k: v for k, v in updates.items() if k in DEFAULTS})
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp_path = CONFIG_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp_path, CONFIG_FILE)
    return config
