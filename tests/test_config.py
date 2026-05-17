"""
Tests for config loading and saving.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kovaaks_gui


class TestLoadConfig:
    def test_load_existing_config(self, tmp_path, sample_config):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(sample_config), encoding="utf-8")

        original = kovaaks_gui.CONFIG_PATH
        kovaaks_gui.CONFIG_PATH = str(cfg_path)
        try:
            loaded = kovaaks_gui.load_config()
            assert loaded["username"] == "testuser"
            assert loaded["min_entries"] == "1000"
        finally:
            kovaaks_gui.CONFIG_PATH = original

    def test_load_missing_config_returns_empty(self, tmp_path):
        original = kovaaks_gui.CONFIG_PATH
        kovaaks_gui.CONFIG_PATH = str(tmp_path / "nonexistent.json")
        try:
            loaded = kovaaks_gui.load_config()
            assert loaded == {}
        finally:
            kovaaks_gui.CONFIG_PATH = original

    def test_load_preserves_all_keys(self, tmp_path):
        cfg = {"username": "u", "custom_key": "custom_value", "min_entries": "500"}
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        original = kovaaks_gui.CONFIG_PATH
        kovaaks_gui.CONFIG_PATH = str(cfg_path)
        try:
            loaded = kovaaks_gui.load_config()
            assert loaded["custom_key"] == "custom_value"
        finally:
            kovaaks_gui.CONFIG_PATH = original


class TestSaveConfig:
    def test_save_creates_file(self, tmp_path, sample_config):
        cfg_path = tmp_path / "config.json"
        original = kovaaks_gui.CONFIG_PATH
        kovaaks_gui.CONFIG_PATH = str(cfg_path)
        try:
            kovaaks_gui.save_config(sample_config)
            assert cfg_path.exists()
            saved = json.loads(cfg_path.read_text(encoding="utf-8"))
            assert saved["username"] == "testuser"
        finally:
            kovaaks_gui.CONFIG_PATH = original

    def test_save_filters_out_password(self, tmp_path):
        """The password key should never be written to disk."""
        cfg = {"username": "user", "password": "secret123", "min_entries": "1000"}
        cfg_path = tmp_path / "config.json"
        original = kovaaks_gui.CONFIG_PATH
        kovaaks_gui.CONFIG_PATH = str(cfg_path)
        try:
            kovaaks_gui.save_config(cfg)
            saved = json.loads(cfg_path.read_text(encoding="utf-8"))
            assert "password" not in saved
            assert saved["username"] == "user"
        finally:
            kovaaks_gui.CONFIG_PATH = original

    def test_save_roundtrip(self, tmp_path, sample_config):
        """Saving and loading should produce the same data (minus password)."""
        cfg_path = tmp_path / "config.json"
        original = kovaaks_gui.CONFIG_PATH
        kovaaks_gui.CONFIG_PATH = str(cfg_path)
        try:
            kovaaks_gui.save_config(sample_config)
            loaded = kovaaks_gui.load_config()
            for key in sample_config:
                if key != "password":
                    assert loaded[key] == sample_config[key]
        finally:
            kovaaks_gui.CONFIG_PATH = original
