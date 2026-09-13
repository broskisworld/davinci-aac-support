"""Unit tests for davinci_aac_support_watch.py's pure logic.

Runs without DaVinci Resolve or real ffmpeg/ffprobe installed -- subprocess
calls are mocked. Only the things that don't need a live Resolve connection
are covered here (has_aac_audio, convert_clip, write_status, process_clip's
branching against a fake clip object).
"""
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import davinci_aac_support_watch as watch  # noqa: E402


@pytest.fixture(autouse=True)
def reset_module_state(tmp_path, monkeypatch):
    """Isolate each test: fresh status file, fresh in-memory caches, fresh
    (default) settings -- never touches the real user config file."""
    watch._status_cache = {}
    watch._fixed_count = 0
    monkeypatch.setattr(watch, "STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setattr(watch, "EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(watch, "NOTIFY", None)  # don't shell out to notify-send in tests
    monkeypatch.setattr(watch.settings, "CONFIG_FILE", str(tmp_path / "config.json"))
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


def _fake_ffmpeg_writing(content=b"converted"):
    def fake_ffmpeg(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:
            f.write(content)
        r = MagicMock()
        r.returncode = 0
        return r
    return fake_ffmpeg


class TestConvertClip:
    def test_success_replaces_original_and_cleans_up_temp(self, tmp_path):
        src = tmp_path / "clip.mov"
        src.write_bytes(b"fake original bytes")

        with patch("subprocess.run", side_effect=_fake_ffmpeg_writing(b"fake converted bytes")):
            assert watch.convert_clip(str(src)) == str(src)

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
            assert watch.convert_clip(str(src)) is None

        assert src.read_bytes() == b"original bytes"
        leftovers = [p for p in tmp_path.iterdir() if p.name != "clip.mov"]
        assert leftovers == [], "temp file should be cleaned up on failure"

    def test_maps_only_video_and_audio_by_default_not_every_stream(self, tmp_path):
        # Regression: "-map 0" (every stream) fails on real files that carry
        # a data-only "tmcd" timecode track -- ffmpeg can't remux it into mp4
        # via stream copy once another stream is being re-encoded ("Could not
        # find tag for codec none in stream #2"), confirmed live. Only video
        # and audio are needed here, and "?" keeps files missing either from
        # erroring. allow_container_change defaults to off, so this is the
        # only path exercised unless a test explicitly turns it on.
        src = tmp_path / "clip.mov"
        src.write_bytes(b"original bytes")

        with patch("subprocess.run", side_effect=_fake_ffmpeg_writing()) as run:
            watch.convert_clip(str(src))

        cmd = run.call_args[0][0]
        assert "-map" in cmd
        assert cmd[cmd.index("-map") + 1] == "0:v?"
        assert cmd.count("-map") == 2 and "0:a?" in cmd, "must map audio too, not just video"
        assert "0" not in [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"], \
            "must not blanket-map every stream ('-map 0') -- that's what let the tmcd bug back in"

    def test_readonly_directory_fails_gracefully(self, tmp_path):
        src = tmp_path / "clip.mov"
        src.write_bytes(b"original bytes")
        with patch("tempfile.mkstemp", side_effect=OSError("Read-only file system")):
            assert watch.convert_clip(str(src)) is None
        assert src.read_bytes() == b"original bytes"

    def test_separate_directory_mode_leaves_original_untouched(self, tmp_path):
        src = tmp_path / "src" / "clip.mov"
        src.parent.mkdir()
        src.write_bytes(b"original bytes")
        out_dir = tmp_path / "fixed"
        watch.settings.save_config({
            "conversion_mode": watch.settings.MODE_SEPARATE_DIRECTORY,
            "output_directory": str(out_dir),
        })

        with patch("subprocess.run", side_effect=_fake_ffmpeg_writing(b"converted bytes")):
            new_path = watch.convert_clip(str(src))

        assert src.read_bytes() == b"original bytes", "the original must never be touched in this mode"
        assert os.path.dirname(new_path) == str(out_dir)
        assert os.path.basename(new_path).startswith("clip-") and new_path.endswith(".mov")
        with open(new_path, "rb") as f:
            assert f.read() == b"converted bytes"

    def test_separate_directory_mode_creates_directory_if_missing(self, tmp_path):
        src = tmp_path / "clip.mov"
        src.write_bytes(b"x")
        out_dir = tmp_path / "does" / "not" / "exist" / "yet"
        watch.settings.save_config({
            "conversion_mode": watch.settings.MODE_SEPARATE_DIRECTORY,
            "output_directory": str(out_dir),
        })

        with patch("subprocess.run", side_effect=_fake_ffmpeg_writing()):
            new_path = watch.convert_clip(str(src))

        assert os.path.isdir(out_dir)
        assert os.path.dirname(new_path) == str(out_dir)

    def test_separate_directory_mode_is_idempotent_across_reprocessing(self, tmp_path):
        src = tmp_path / "clip.mov"
        src.write_bytes(b"x")
        out_dir = tmp_path / "fixed"
        watch.settings.save_config({
            "conversion_mode": watch.settings.MODE_SEPARATE_DIRECTORY,
            "output_directory": str(out_dir),
        })

        with patch("subprocess.run", side_effect=_fake_ffmpeg_writing()):
            path1 = watch.convert_clip(str(src))
            path2 = watch.convert_clip(str(src))

        assert path1 == path2, "reprocessing the same source should converge on the same output file"

    def test_allow_container_change_off_never_probes_extra_streams(self, tmp_path):
        src = tmp_path / "clip.mov"
        src.write_bytes(b"x")
        watch.settings.save_config({"allow_container_change": False})

        with patch.object(watch, "get_non_av_stream_types") as probe, \
             patch("subprocess.run", side_effect=_fake_ffmpeg_writing()):
            watch.convert_clip(str(src))

        probe.assert_not_called()

    def test_allow_container_change_on_uses_full_preserve_when_extra_stream_present(self, tmp_path):
        src = tmp_path / "clip.mov"
        src.write_bytes(b"x")
        watch.settings.save_config({"allow_container_change": True})

        with patch.object(watch, "get_non_av_stream_types", return_value=["data"]), \
             patch("subprocess.run", side_effect=_fake_ffmpeg_writing()) as run:
            watch.convert_clip(str(src))

        cmd = run.call_args[0][0]
        assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "mov"
        assert cmd[cmd.index("-map") + 1] == "0", "must map every stream to keep the extra one"

    def test_allow_container_change_on_but_no_extra_streams_uses_safe_default(self, tmp_path):
        src = tmp_path / "clip.mov"
        src.write_bytes(b"x")
        watch.settings.save_config({"allow_container_change": True})

        with patch.object(watch, "get_non_av_stream_types", return_value=[]), \
             patch("subprocess.run", side_effect=_fake_ffmpeg_writing()) as run:
            watch.convert_clip(str(src))

        cmd = run.call_args[0][0]
        assert "-f" not in cmd, "no extra stream to preserve -- no reason to change the container brand"

    def test_full_preserve_attempt_falls_back_to_safe_default_on_failure(self, tmp_path):
        src = tmp_path / "clip.mov"
        src.write_bytes(b"original")
        watch.settings.save_config({"allow_container_change": True})

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            r = MagicMock()
            if "-f" in cmd:  # the full-preserve attempt: simulate it failing
                r.returncode = 1
                r.stderr = "muxer rejected it"
            else:
                r.returncode = 0
                with open(cmd[-1], "wb") as f:
                    f.write(b"converted")
            return r

        with patch.object(watch, "get_non_av_stream_types", return_value=["data"]), \
             patch("subprocess.run", side_effect=fake_run):
            result = watch.convert_clip(str(src))

        assert result == str(src)
        assert len(calls) == 2, "should try full-preserve first, then fall back -- not give up"
        assert src.read_bytes() == b"converted"


class TestHashedOutputPath:
    def test_deterministic_for_same_source_path(self):
        p1 = watch._hashed_output_path("/a/b/clip.mov", "/out")
        p2 = watch._hashed_output_path("/a/b/clip.mov", "/out")
        assert p1 == p2

    def test_different_sources_with_same_basename_do_not_collide(self):
        p1 = watch._hashed_output_path("/a/clip.mov", "/out")
        p2 = watch._hashed_output_path("/b/clip.mov", "/out")
        assert p1 != p2

    def test_keeps_original_extension_and_output_directory(self):
        p = watch._hashed_output_path("/a/b/clip.mov", "/out")
        assert os.path.dirname(p) == "/out"
        assert p.endswith(".mov")


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

    def test_replace_clip_failure_is_cached_as_failed_not_fixed(self, tmp_path):
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

        assert watch._status_cache["uid-3"] == "failed"
        assert watch._fixed_count == 0

    def test_failed_clip_is_not_retried_or_re_notified_every_poll(self, tmp_path):
        # Regression: previously neither failure branch cached anything, so
        # a clip that failed once got retried -- and re-fired the "AAC
        # Support failed" notification -- every single poll interval
        # forever. Confirmed live as a real notification-spam bug against a
        # file with a permanently-failing conversion.
        src = tmp_path / "aac.mov"
        src.write_bytes(b"original")
        clip = _fake_clip("uid-6", str(src))

        r = MagicMock()
        r.returncode = 1
        r.stderr = "ffmpeg blew up"

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return _ffprobe_result("aac\n")
            return r

        notify_calls = []
        with patch("subprocess.run", side_effect=fake_run), \
             patch.object(watch, "notify", side_effect=lambda *a: notify_calls.append(a)):
            watch.process_clip(clip)
            watch.process_clip(clip)  # second poll should short-circuit

        assert watch._status_cache["uid-6"] == "failed"
        assert len(notify_calls) == 1, "should only notify once per clip, not every poll"

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
