"""Unit tests for davinci_aac_support_config.py -- pure file I/O, no Resolve
or ffmpeg involved.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import davinci_aac_support_config as config  # noqa: E402


class TestLoadConfig:
    def test_missing_file_returns_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "nope.json"))
        assert config.load_config() == config.DEFAULTS

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path, monkeypatch):
        f = tmp_path / "config.json"
        f.write_text("{not valid json")
        monkeypatch.setattr(config, "CONFIG_FILE", str(f))
        assert config.load_config() == config.DEFAULTS

    def test_partial_file_is_merged_with_defaults(self, tmp_path, monkeypatch):
        f = tmp_path / "config.json"
        f.write_text(json.dumps({"conversion_mode": "separate_directory"}))
        monkeypatch.setattr(config, "CONFIG_FILE", str(f))
        loaded = config.load_config()
        assert loaded["conversion_mode"] == "separate_directory"
        assert loaded["output_directory"] == config.DEFAULTS["output_directory"]
        assert loaded["allow_container_change"] == config.DEFAULTS["allow_container_change"]

    def test_unknown_keys_in_file_are_ignored(self, tmp_path, monkeypatch):
        f = tmp_path / "config.json"
        f.write_text(json.dumps({"conversion_mode": "in_place", "made_up_key": "x"}))
        monkeypatch.setattr(config, "CONFIG_FILE", str(f))
        assert "made_up_key" not in config.load_config()


class TestSaveConfig:
    def test_persists_across_reload(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "config.json"))
        config.save_config({"conversion_mode": "separate_directory", "output_directory": "/tmp/x"})
        assert config.load_config()["conversion_mode"] == "separate_directory"
        assert config.load_config()["output_directory"] == "/tmp/x"

    def test_merges_rather_than_overwriting_other_settings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "config.json"))
        config.save_config({"allow_container_change": True})
        config.save_config({"conversion_mode": "separate_directory"})
        loaded = config.load_config()
        assert loaded["allow_container_change"] is True
        assert loaded["conversion_mode"] == "separate_directory"

    def test_unknown_keys_are_rejected_not_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "config.json"))
        config.save_config({"made_up_key": "x"})
        assert "made_up_key" not in config.load_config()

    def test_creates_parent_directory_if_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "sub" / "config.json"))
        config.save_config({"conversion_mode": "separate_directory"})
        assert os.path.isfile(tmp_path / "sub" / "config.json")

    def test_no_stray_temp_file_left_behind(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "config.json"))
        config.save_config({"conversion_mode": "separate_directory"})
        assert sorted(os.listdir(tmp_path)) == ["config.json"]
