"""Unit tests for resolve_aac_watch.py's pure logic.

Runs without DaVinci Resolve or real ffmpeg/ffprobe installed -- subprocess
calls are mocked. Only the things that don't need a live Resolve connection
are covered here (has_aac_audio, convert_in_place, write_status,
process_clip's branching against a fake clip object).
"""
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import resolve_aac_watch as watch  # noqa: E402


@pytest.fixture(autouse=True)
def reset_module_state(tmp_path, monkeypatch):
    """Isolate each test: fresh status file, fresh in-memory caches."""
    watch._status_cache = {}
    watch._fixed_count = 0
    monkeypatch.setattr(watch, "STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setattr(watch, "NOTIFY", None)  # don't shell out to notify-send in tests
    yield


def _ffprobe_result(stdout):
    r = MagicMock()
    r.stdout = stdout
    return r


class TestHasAacAudio:
    def test_detects_aac(self):
        with patch("subprocess.run", return_value=_ffprobe_result("aac\n")):
            assert watch.has_aac_audio("/some/file.mov") is True

    def test_no_aac_when_pcm(self):
        with patch("subprocess.run", return_value=_ffprobe_result("pcm_s16le\n")):
            assert watch.has_aac_audio("/some/file.mov") is False

    def test_no_audio_streams_at_all(self):
        with patch("subprocess.run", return_value=_ffprobe_result("")):
            assert watch.has_aac_audio("/some/file.mov") is False

    def test_mixed_streams_any_aac_counts(self):
        with patch("subprocess.run", return_value=_ffprobe_result("pcm_s16le\naac\n")):
            assert watch.has_aac_audio("/some/file.mov") is True

    def test_ffprobe_missing_or_erroring_is_treated_as_no_aac(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("no ffprobe")):
            assert watch.has_aac_audio("/some/file.mov") is False

    def test_ffprobe_timeout_is_treated_as_no_aac(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
            assert watch.has_aac_audio("/some/file.mov") is False


class TestConvertInPlace:
    def test_success_replaces_original_and_cleans_up_temp(self, tmp_path):
        src = tmp_path / "clip.mov"
        src.write_bytes(b"fake original bytes")

        def fake_ffmpeg(cmd, **kwargs):
            # ffmpeg's real job is "write real output to the temp path" --
            # simulate that so the subsequent os.replace has something to swap in.
            tmp_out = cmd[-1]
            with open(tmp_out, "wb") as f:
                f.write(b"fake converted bytes")
            r = MagicMock()
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=fake_ffmpeg):
            assert watch.convert_in_place(str(src)) is True

        assert src.read_bytes() == b"fake converted bytes"
        # no stray temp files left in the directory
        leftovers = [p for p in tmp_path.iterdir() if p.name != "clip.mov"]
        assert leftovers == []

    def test_ffmpeg_failure_leaves_original_untouched(self, tmp_path):
        src = tmp_path / "clip.mov"
        src.write_bytes(b"original bytes")

        r = MagicMock()
        r.returncode = 1
        r.stderr = "ffmpeg blew up"
        with patch("subprocess.run", return_value=r):
            assert watch.convert_in_place(str(src)) is False

        assert src.read_bytes() == b"original bytes"
        leftovers = [p for p in tmp_path.iterdir() if p.name != "clip.mov"]
        assert leftovers == [], "temp file should be cleaned up on failure"

    def test_readonly_directory_fails_gracefully(self, tmp_path):
        src = tmp_path / "clip.mov"
        src.write_bytes(b"original bytes")
        with patch("tempfile.mkstemp", side_effect=OSError("Read-only file system")):
            assert watch.convert_in_place(str(src)) is False
        assert src.read_bytes() == b"original bytes"


class TestWriteStatus:
    def test_creates_file_with_fields(self, tmp_path, monkeypatch):
        status_file = tmp_path / "sub" / "status.json"
        monkeypatch.setattr(watch, "STATUS_FILE", str(status_file))
        watch.write_status(connected=True, project="My Project")
        data = json.loads(status_file.read_text())
        assert data["connected"] is True
        assert data["project"] == "My Project"
        assert "last_update" in data

    def test_merges_rather_than_overwrites(self, tmp_path, monkeypatch):
        status_file = tmp_path / "status.json"
        monkeypatch.setattr(watch, "STATUS_FILE", str(status_file))
        watch.write_status(connected=True, product="Resolve")
        watch.write_status(project="New Project")
        data = json.loads(status_file.read_text())
        assert data["connected"] is True
        assert data["product"] == "Resolve"
        assert data["project"] == "New Project"

    def test_survives_corrupt_existing_file(self, tmp_path, monkeypatch):
        status_file = tmp_path / "status.json"
        status_file.write_text("{not valid json")
        monkeypatch.setattr(watch, "STATUS_FILE", str(status_file))
        watch.write_status(connected=False)
        data = json.loads(status_file.read_text())
        assert data["connected"] is False


def _fake_clip(uid, path, name=None, replace_clip_result=True):
    clip = MagicMock()
    clip.GetUniqueId.return_value = uid
    props = {"File Path": path, "File Name": name or os.path.basename(path)}
    clip.GetClipProperty.side_effect = lambda k=None: props.get(k)
    clip.ReplaceClip.return_value = replace_clip_result
    return clip


class TestProcessClip:
    def test_clean_clip_marked_and_skipped_next_time(self, tmp_path):
        src = tmp_path / "clean.mov"
        src.write_bytes(b"x")
        clip = _fake_clip("uid-1", str(src))

        with patch("subprocess.run", return_value=_ffprobe_result("pcm_s16le\n")) as run:
            watch.process_clip(clip)
            watch.process_clip(clip)  # second pass should short-circuit

        assert watch._status_cache["uid-1"] == "clean"
        assert run.call_count == 1, "second pass should skip ffprobe entirely (cached)"
        clip.ReplaceClip.assert_not_called()

    def test_aac_clip_gets_fixed_and_counted(self, tmp_path):
        src = tmp_path / "aac.mov"
        src.write_bytes(b"original")
        clip = _fake_clip("uid-2", str(src))

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return _ffprobe_result("aac\n")
            tmp_out = cmd[-1]
            with open(tmp_out, "wb") as f:
                f.write(b"converted")
            r = MagicMock()
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=fake_run):
            watch.process_clip(clip)

        clip.ReplaceClip.assert_called_once_with(str(src))
        assert watch._status_cache["uid-2"] == "fixed"
        assert watch._fixed_count == 1
        assert src.read_bytes() == b"converted"

    def test_replace_clip_failure_is_not_cached_as_fixed(self, tmp_path):
        src = tmp_path / "aac.mov"
        src.write_bytes(b"original")
        clip = _fake_clip("uid-3", str(src), replace_clip_result=False)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return _ffprobe_result("aac\n")
            tmp_out = cmd[-1]
            with open(tmp_out, "wb") as f:
                f.write(b"converted")
            r = MagicMock()
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=fake_run):
            watch.process_clip(clip)

        assert "uid-3" not in watch._status_cache, "should retry on next poll, not get stuck"
        assert watch._fixed_count == 0

    def test_missing_file_is_skipped_silently(self):
        clip = _fake_clip("uid-4", "/does/not/exist.mov")
        with patch("subprocess.run") as run:
            watch.process_clip(clip)
        run.assert_not_called()
        assert "uid-4" not in watch._status_cache

    def test_empty_file_path_is_skipped(self):
        # Timelines and other non-file media pool items report an empty path.
        clip = _fake_clip("uid-5", "")
        with patch("subprocess.run") as run:
            watch.process_clip(clip)
        run.assert_not_called()
