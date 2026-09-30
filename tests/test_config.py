"""
Tests for config loading and saving.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import kovaaks.config_helpers as config_helpers


@pytest.fixture(autouse=True)
def patch_config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config_helpers, "CONFIG_PATH", str(tmp_path / "config.json"))


class TestLoadConfig:
    def test_load_existing_config(self, sample_config):
        with open(config_helpers.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(sample_config, f)
        loaded = config_helpers.load_config()
        assert loaded["username"] == "testuser"
        assert loaded["min_entries"] == "1000"

    def test_load_missing_config_returns_empty(self):
        assert config_helpers.load_config() == {}

    def test_load_preserves_all_keys(self):
        cfg = {"username": "u", "custom_key": "custom_value", "min_entries": "500"}
        with open(config_helpers.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        loaded = config_helpers.load_config()
        assert loaded["custom_key"] == "custom_value"

    @pytest.mark.parametrize("contents", [
        b'{"username":', b"\xff", b"[]", b"null", b'"username"',
    ])
    def test_invalid_config_returns_empty_and_preserves_file(self, contents, caplog):
        """A damaged config should not prevent startup or be overwritten on load."""
        with open(config_helpers.CONFIG_PATH, "wb") as f:
            f.write(contents)

        assert config_helpers.load_config() == {}
        with open(config_helpers.CONFIG_PATH, "rb") as f:
            assert f.read() == contents
        assert "Could not load config" in caplog.text

    def test_unreadable_config_returns_empty(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(config_helpers, "CONFIG_PATH", str(tmp_path))

        assert config_helpers.load_config() == {}
        assert "Could not load config" in caplog.text


class TestSaveConfig:
    def test_save_creates_file(self, sample_config):
        config_helpers.save_config(sample_config)
        assert os.path.exists(config_helpers.CONFIG_PATH)
        with open(config_helpers.CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["username"] == "testuser"

    def test_save_filters_out_password(self):
        """The password key should never be written to disk."""
        cfg = {"username": "user", "password": "secret123", "min_entries": "1000"}
        config_helpers.save_config(cfg)
        with open(config_helpers.CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert "password" not in saved
        assert saved["username"] == "user"

    def test_save_roundtrip(self, sample_config):
        """Saving and loading should produce the same data (minus password)."""
        config_helpers.save_config(sample_config)
        loaded = config_helpers.load_config()
        for key in sample_config:
            if key != "password":
                assert loaded[key] == sample_config[key]

    def test_serialization_failure_preserves_previous_config(self, tmp_path):
        config_helpers.save_config({"username": "retained"})
        with open(config_helpers.CONFIG_PATH, "rb") as f:
            previous = f.read()

        with pytest.raises(TypeError):
            config_helpers.save_config({"username": "replacement", "bad": object()})

        with open(config_helpers.CONFIG_PATH, "rb") as f:
            assert f.read() == previous
        assert list(tmp_path.iterdir()) == [tmp_path / "config.json"]

    @pytest.mark.parametrize("failure_stage", ["write", "replace"])
    def test_disk_failure_preserves_previous_config(self, tmp_path, monkeypatch, failure_stage):
        config_helpers.save_config({"username": "retained"})
        with open(config_helpers.CONFIG_PATH, "rb") as f:
            previous = f.read()

        def fail_write(cfg, file, **kwargs):
            file.write('{"username":')
            raise OSError("disk full")

        def fail_replace(source, destination):
            raise OSError("replacement blocked")

        if failure_stage == "write":
            monkeypatch.setattr(config_helpers.json, "dump", fail_write)
        else:
            monkeypatch.setattr(config_helpers.os, "replace", fail_replace)

        with pytest.raises(OSError):
            config_helpers.save_config({"username": "replacement"})

        with open(config_helpers.CONFIG_PATH, "rb") as f:
            assert f.read() == previous
        assert list(tmp_path.iterdir()) == [tmp_path / "config.json"]

    def test_uses_unique_temporary_file_in_config_directory(self, tmp_path, monkeypatch):
        original_replace = config_helpers.os.replace
        temporary_paths = []

        def record_replace(source, destination):
            assert os.path.dirname(source) == str(tmp_path)
            assert f".{os.getpid()}." in os.path.basename(source)
            temporary_paths.append(source)
            original_replace(source, destination)

        monkeypatch.setattr(config_helpers.os, "replace", record_replace)
        config_helpers.save_config({"username": "first"})
        config_helpers.save_config({"username": "second"})

        assert temporary_paths[0] != temporary_paths[1]
        assert config_helpers.load_config() == {"username": "second"}
        assert list(tmp_path.iterdir()) == [tmp_path / "config.json"]
