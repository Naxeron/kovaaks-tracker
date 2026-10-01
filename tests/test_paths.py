"""Runtime data layout and non-destructive upgrades from root-level files."""

import json
import logging
import os
from types import SimpleNamespace

import pytest

from kovaaks import config_helpers, logging_helpers, paths


@pytest.fixture
def config_paths(tmp_path, monkeypatch):
    """Keep both sides of every migration inside this test's directory."""
    current = tmp_path / "data" / "config.json"
    legacy = tmp_path / "config.json"
    monkeypatch.setattr(config_helpers, "CONFIG_PATH", str(current))
    return current, legacy


def test_valid_legacy_config_moves_without_losing_password(config_paths):
    current, legacy = config_paths
    contents = '{"username": "alice", "password": "legacy-secret", "min_entries": 321}'
    legacy.write_text(contents, encoding="utf-8")
    legacy.chmod(0o600)

    assert config_helpers.load_config() == json.loads(contents)
    assert current.read_text(encoding="utf-8") == contents
    assert not legacy.exists()
    assert current.stat().st_mode & 0o777 == 0o600

    # The existing secure-storage workflow can still read the old password,
    # then remove it with the ordinary atomic config save.
    config_helpers.save_config(json.loads(contents))
    assert json.loads(current.read_text(encoding="utf-8")) == {
        "username": "alice", "min_entries": 321,
    }
    assert not legacy.exists()


def test_current_config_wins_and_legacy_is_untouched(config_paths):
    current, legacy = config_paths
    current.parent.mkdir()
    current.write_text('{"username": "current"}', encoding="utf-8")
    legacy.write_text('{"username": "legacy"}', encoding="utf-8")

    assert config_helpers.load_config() == {"username": "current"}
    assert json.loads(legacy.read_text(encoding="utf-8")) == {"username": "legacy"}


@pytest.mark.parametrize("contents", [b'{"username":', b"\xff", b"[]", b"null"])
def test_corrupt_legacy_config_stays_intact(config_paths, contents):
    current, legacy = config_paths
    legacy.write_bytes(contents)

    assert config_helpers.load_config() == {}
    assert legacy.read_bytes() == contents
    assert not current.exists()


def test_unreadable_legacy_config_stays_intact(config_paths):
    current, legacy = config_paths
    legacy.mkdir()

    assert config_helpers.load_config() == {}
    assert legacy.is_dir()
    assert not current.exists()


def test_corrupt_current_config_does_not_load_old_settings(config_paths):
    current, legacy = config_paths
    current.parent.mkdir()
    current.write_bytes(b"broken config")
    legacy.write_text('{"username": "legacy"}', encoding="utf-8")

    assert config_helpers.load_config() == {}
    assert current.read_bytes() == b"broken config"
    assert legacy.exists()


def test_save_creates_data_directory(config_paths):
    current, legacy = config_paths

    config_helpers.save_config({"username": "alice", "password": "secret"})

    assert json.loads(current.read_text(encoding="utf-8")) == {"username": "alice"}
    assert not legacy.exists()


def test_missing_config_load_has_no_filesystem_side_effects(config_paths):
    current, legacy = config_paths

    assert config_helpers.load_config() == {}
    assert not current.parent.exists()
    assert not legacy.exists()


def test_failed_migration_keeps_saves_and_password_cleanup_on_legacy(config_paths, monkeypatch):
    current, legacy = config_paths
    legacy.write_text('{"username": "alice", "password": "legacy-secret"}', encoding="utf-8")

    def fail_link(*args):
        raise PermissionError("migration blocked")

    monkeypatch.setattr(paths.os, "link", fail_link)

    cfg = config_helpers.load_config()
    assert cfg == {"username": "alice", "password": "legacy-secret"}
    assert config_helpers.CONFIG_PATH == str(legacy)
    assert not current.exists()

    config_helpers.save_config(cfg)
    assert json.loads(legacy.read_text(encoding="utf-8")) == {"username": "alice"}
    assert not current.exists()


def test_existing_destination_created_during_migration_wins(config_paths, monkeypatch):
    current, legacy = config_paths
    legacy.write_text('{"username": "legacy"}', encoding="utf-8")
    real_link = paths.os.link

    def concurrent_link(source, destination):
        current.write_text('{"username": "current"}', encoding="utf-8")
        real_link(source, destination)

    monkeypatch.setattr(paths.os, "link", concurrent_link)

    assert config_helpers.load_config() == {"username": "current"}
    assert json.loads(current.read_text(encoding="utf-8")) == {"username": "current"}
    assert json.loads(legacy.read_text(encoding="utf-8")) == {"username": "legacy"}


@pytest.mark.parametrize("contents, expected", [
    ('{"username": "alice"}', {"username": "alice"}),
    ('{"username":', {}),
])
def test_legacy_moves_before_config_read_retries_current(config_paths, monkeypatch, contents, expected):
    current, legacy = config_paths
    legacy.write_text(contents, encoding="utf-8")
    real_read = config_helpers._read_config

    def migrate_before_read(path):
        if path == str(legacy):
            current.parent.mkdir()
            legacy.rename(current)
        return real_read(path)

    monkeypatch.setattr(config_helpers, "_read_config", migrate_before_read)

    assert config_helpers.load_config() == expected
    assert current.read_text(encoding="utf-8") == contents
    assert not legacy.exists()


def test_unlink_failure_removes_new_link_and_keeps_original(config_paths, monkeypatch):
    current, legacy = config_paths
    legacy.write_text('{"username": "alice", "password": "legacy-secret"}', encoding="utf-8")
    real_unlink = paths.os.unlink

    def fail_legacy_unlink(path):
        if os.fspath(path) == str(legacy):
            raise PermissionError("legacy file is locked")
        real_unlink(path)

    monkeypatch.setattr(paths.os, "unlink", fail_legacy_unlink)

    assert config_helpers.load_config()["password"] == "legacy-secret"
    assert config_helpers.CONFIG_PATH == str(legacy)
    assert not current.exists()
    assert legacy.exists()


def test_custom_paths_do_not_search_unrelated_legacy_files(tmp_path):
    legacy = tmp_path / "config.json"
    legacy.write_text('{"username": "legacy"}', encoding="utf-8")
    custom = tmp_path / "custom" / "config.json"

    assert paths.existing_data_path(custom) == str(custom)


def test_relative_symlink_keeps_original_target(config_paths):
    current, legacy = config_paths
    actual = legacy.with_name("actual-config.json")
    actual.write_text('{"username": "alice"}', encoding="utf-8")
    try:
        legacy.symlink_to(actual.name)
    except OSError:
        pytest.skip("This platform does not permit symlink creation")

    assert config_helpers.load_config() == {"username": "alice"}
    assert config_helpers.CONFIG_PATH == str(legacy)
    assert legacy.is_symlink()
    assert not current.exists()


@pytest.fixture
def isolated_logging(tmp_path, monkeypatch):
    """Use a private logger and close file handles before pytest removes files."""
    current = tmp_path / "data" / "kovaaks.log"
    legacy = tmp_path / "kovaaks.log"
    logger = logging.Logger("kovaaks-path-tests")
    monkeypatch.setattr(logging_helpers, "LOG_FILE", str(current))
    # Leave pytest's own root logger and capture handlers untouched.
    private_logging = SimpleNamespace(**vars(logging))
    private_logging.getLogger = lambda *args: logger
    monkeypatch.setattr(logging_helpers, "logging", private_logging)
    try:
        yield current, legacy, logger
    finally:
        for handler in logger.handlers:
            handler.close()


def test_logging_creates_data_directory(isolated_logging):
    current, legacy, logger = isolated_logging

    assert logging_helpers.setup_logging() is logger

    assert current.is_file()
    assert not legacy.exists()


def test_logging_migrates_previous_launch_history(isolated_logging):
    current, legacy, _ = isolated_logging
    legacy.write_text("previous launch information\n", encoding="utf-8")

    logging_helpers.setup_logging()

    assert "previous launch information" in current.read_text(encoding="utf-8")
    assert not legacy.exists()


def test_current_log_wins_over_legacy(isolated_logging):
    current, legacy, _ = isolated_logging
    current.parent.mkdir()
    current.write_text("current information\n", encoding="utf-8")
    legacy.write_text("legacy information\n", encoding="utf-8")

    logging_helpers.setup_logging()

    assert "current information" in current.read_text(encoding="utf-8")
    assert "legacy information" not in current.read_text(encoding="utf-8")
    assert legacy.read_text(encoding="utf-8") == "legacy information\n"


def test_logging_migration_failure_keeps_original_log(isolated_logging, monkeypatch):
    current, legacy, logger = isolated_logging
    legacy.write_text("previous launch information\n", encoding="utf-8")

    def fail_link(*args):
        raise PermissionError("migration blocked")

    monkeypatch.setattr(paths.os, "link", fail_link)

    logging_helpers.setup_logging()
    logger.info("new information")

    assert logging_helpers.LOG_FILE == str(legacy)
    assert not current.exists()
    assert "previous launch information" in legacy.read_text(encoding="utf-8")
    assert "new information" in legacy.read_text(encoding="utf-8")
