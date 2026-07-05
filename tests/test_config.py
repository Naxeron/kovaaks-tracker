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
