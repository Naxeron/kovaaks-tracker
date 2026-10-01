"""Credential bridge regressions with isolated config files and a fake OS store."""

import gzip
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kovaaks import config_helpers, credentials, fetch_worker
from kovaaks import app as kovaaks_web


class SyncThread:
    """Run finite fetch wrappers inline; no polling tasks are started here."""

    def __init__(self, target, args=(), kwargs=None, daemon=True):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


@pytest.fixture
def make_api(monkeypatch, tmp_path, isolated_credentials):
    """Exercise real config serialization without loading caches or user stats."""
    monkeypatch.setattr(kovaaks_web, "load_scores_cache", lambda: {
        "scenarios": [], "scores": {}, "entry_history": {},
    })
    monkeypatch.setattr(kovaaks_web, "_get_local_stats", lambda *args: {})
    monkeypatch.setattr(kovaaks_web.KovaaksAPI, "_start_file_watcher", lambda self: None)

    def create(config=None):
        cfg = {"username": "alice", "stats_dir": str(tmp_path / "stats")}
        cfg.update(config or {})
        Path(config_helpers.CONFIG_PATH).write_text(json.dumps(cfg), encoding="utf-8")
        return kovaaks_web.KovaaksAPI()

    return create


def read_config():
    return json.loads(Path(config_helpers.CONFIG_PATH).read_text(encoding="utf-8"))


def fail_storage(*args):
    raise credentials.CredentialStorageError("backend-error-with-secret-password")


def test_startup_restores_saved_password_without_exposing_it(make_api, isolated_credentials):
    isolated_credentials["alice"] = "stored-secret"
    api = make_api()

    assert api._credentials_loaded_event.is_set()
    assert api._get_login_credentials() == ("alice", "stored-secret")
    public_config = api.get_config()
    assert public_config["has_password"] is True
    assert public_config["credential_storage"] == "saved"
    assert "password" not in public_config
    assert "password" not in api._cfg
    assert "stored-secret" not in json.dumps(public_config)
    assert "stored-secret" not in Path(config_helpers.CONFIG_PATH).read_text()


def test_table_config_does_not_wait_for_initial_credential_unlock(make_api, isolated_credentials):
    isolated_credentials["alice"] = "stored-secret"
    api = make_api({"min_entries": 321, "column_widths": {"Scenario": 240}})
    api._credentials_loaded_event.clear()
    try:
        # A nonblocking call must not even attempt the wait or credential lock.
        api._credentials_loaded_event.wait = MagicMock(side_effect=AssertionError("credential wait"))
        config = api.get_config(False)
        assert config["credentials_pending"] is True
        assert config["username"] == "alice"
        assert config["min_entries"] == 321
        assert config["column_widths"] == {"Scenario": 240}
        assert "has_password" not in config
        assert "password" not in config
        assert "stored-secret" not in json.dumps(config)
    finally:
        api._credentials_loaded_event.set()

    config = api.get_config(False)
    assert config["credentials_pending"] is False
    assert config["has_password"] is True


def test_table_config_does_not_wait_for_busy_credential_lock(make_api):
    api = make_api()
    locked, release = threading.Event(), threading.Event()

    def hold_credentials():
        with api._credentials_lock:
            locked.set()
            assert release.wait(5)

    worker = threading.Thread(target=hold_credentials, daemon=True)
    worker.start()
    try:
        assert locked.wait(5)
        config = api.get_config(False)
        assert config["credentials_pending"] is True
        assert "has_password" not in config
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert api.get_config()["credentials_pending"] is False


def test_blank_password_preserves_stored_secret_and_authenticated_session(make_api, isolated_credentials):
    isolated_credentials["alice"] = "stored-secret"
    api = make_api()
    api._jwt_token = "existing-session"
    settings = {"username": "alice", "password": "", "min_entries": 1234}

    result = api.save_settings(settings)

    assert result["ok"] is True
    assert api._get_login_credentials() == ("alice", "stored-secret")
    assert api._jwt_token == "existing-session"
    assert isolated_credentials == {"alice": "stored-secret"}
    assert read_config()["min_entries"] == 1234
    assert "password" not in read_config()
    assert settings["password"] == ""  # The bridge must not mutate caller input.


def test_saving_new_password_replaces_store_and_invalidates_session(make_api, isolated_credentials):
    isolated_credentials["alice"] = "old-secret"
    api = make_api()
    api._jwt_token = "old-session"

    result = api.save_credentials(" alice ", " new secret with spaces ")

    assert result["ok"] is True
    assert result["credential_storage"] == "saved"
    assert isolated_credentials["alice"] == " new secret with spaces "
    assert api._get_login_credentials() == ("alice", " new secret with spaces ")
    assert api._jwt_token is None
    assert "password" not in api._cfg
    assert "password" not in read_config()
    assert "new secret" not in json.dumps(result)


@pytest.mark.parametrize("next_password", [None, "bob-secret"])
def test_account_switch_never_reuses_previous_accounts_password(make_api, isolated_credentials, next_password):
    isolated_credentials["alice"] = "alice-secret"
    if next_password:
        isolated_credentials["bob"] = next_password
    api = make_api()
    api._jwt_token = "alice-session"

    result = api.save_settings({"username": "bob", "password": ""})

    assert result["ok"] is True
    assert api._get_login_credentials() == ("bob", next_password or "")
    assert api.get_config()["has_password"] is bool(next_password)
    assert api._jwt_token is None
    assert isolated_credentials["alice"] == "alice-secret"
    assert read_config()["username"] == "bob"


def test_unavailable_storage_allows_session_login_without_persisting_secret(make_api, monkeypatch, caplog):
    monkeypatch.setattr(credentials, "get_password", fail_storage)
    monkeypatch.setattr(credentials, "set_password", fail_storage)
    api = make_api()
    assert api.get_config()["credential_storage"] == "unavailable"

    result = api.save_credentials("alice", "session-secret")
    worker = MagicMock()
    monkeypatch.setattr(kovaaks_web, "run_fetch_all", worker)
    monkeypatch.setattr(kovaaks_web.threading, "Thread", SyncThread)

    assert result["ok"] is True
    assert result["credential_storage"] == "session"
    assert "session" in result["message"]
    assert api.fetch_all_stats() is True
    worker.assert_called_once_with(api, "alice", "session-secret", False)
    assert "password" not in api._cfg
    assert "session-secret" not in Path(config_helpers.CONFIG_PATH).read_text()
    assert "backend-error-with-secret-password" not in json.dumps(api.get_config()) + json.dumps(result) + caplog.text


def test_successful_legacy_migration_strips_plaintext_from_config(make_api, isolated_credentials):
    api = make_api({"password": "legacy-secret", "min_entries": 321})

    assert isolated_credentials["alice"] == "legacy-secret"
    assert api._get_login_credentials() == ("alice", "legacy-secret")
    assert api._legacy_migration_pending is False
    assert api.get_config()["credential_storage"] == "saved"
    assert "password" not in api._cfg
    assert "password" not in read_config()
    assert read_config()["min_entries"] == 321


def test_failed_legacy_migration_preserves_file_during_settings_and_metadata_updates(make_api, monkeypatch):
    monkeypatch.setattr(credentials, "set_password", fail_storage)
    api = make_api({"password": "legacy-secret", "min_entries": 321})
    original = Path(config_helpers.CONFIG_PATH).read_bytes()

    result = api.save_settings({"password": "", "min_entries": 456})
    payload = [{"leaderboardId": "one", "counts": {"entries": 1000}}]
    compressed = gzip.compress(json.dumps(payload).encode())
    response = SimpleNamespace(
        status_code=200, headers={"ETag": "new-etag"},
        iter_content=lambda chunk_size: iter([compressed]), close=lambda: None,
    )
    monkeypatch.setattr(fetch_worker, "api_request_with_retry", lambda *args, **kwargs: response)

    assert fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", api) == payload
    assert api._cfg["last_etags"]["scenarios.json.gz"] == "new-etag"
    assert result["ok"] is False
    assert api._legacy_migration_pending is True
    assert api._get_login_credentials() == ("alice", "legacy-secret")
    assert "password" not in api._cfg
    assert Path(config_helpers.CONFIG_PATH).read_bytes() == original


def test_failed_legacy_migration_can_be_retried_and_then_saved(make_api, monkeypatch, isolated_credentials):
    monkeypatch.setattr(credentials, "set_password", fail_storage)
    api = make_api({"password": "legacy-secret"})
    assert api._legacy_migration_pending is True
    monkeypatch.setattr(credentials, "set_password", lambda user, secret: isolated_credentials.__setitem__(user, secret))

    result = api.save_credentials("alice", "replacement-secret")

    assert result["ok"] is True
    assert api._legacy_migration_pending is False
    assert isolated_credentials["alice"] == "replacement-secret"
    assert "password" not in read_config()


def test_failed_legacy_migration_rejects_account_switch_without_destroying_secret(make_api, monkeypatch):
    monkeypatch.setattr(credentials, "set_password", fail_storage)
    api = make_api({"password": "legacy-secret"})
    original = Path(config_helpers.CONFIG_PATH).read_bytes()

    result = api.save_settings({"username": "bob", "password": "bob-secret"})

    assert result["ok"] is False
    assert api._get_login_credentials() == ("alice", "legacy-secret")
    assert Path(config_helpers.CONFIG_PATH).read_bytes() == original


def test_legacy_session_fallback_does_not_mask_an_active_fetch_error(make_api, monkeypatch):
    monkeypatch.setattr(credentials, "set_password", fail_storage)
    api = make_api({"password": "legacy-secret"})
    api._fetch_in_progress = True

    result = api.save_credentials("alice", "legacy-secret")

    assert result["ok"] is False
    assert "current fetch" in result["message"]
    assert read_config()["password"] == "legacy-secret"


def test_legacy_config_cleanup_failure_keeps_recoverable_store_and_safe_message(make_api, monkeypatch, isolated_credentials):
    save_config = config_helpers.save_config
    monkeypatch.setattr(config_helpers, "save_config", MagicMock(side_effect=OSError("file-error-secret")))
    api = make_api({"password": "legacy-secret"})

    assert isolated_credentials["alice"] == "legacy-secret"
    assert api._credentials_loaded_event.is_set()
    assert api.get_config()["credential_storage"] == "saved"
    assert api.get_config()["credential_warning"] is True
    assert "file-error-secret" not in json.dumps(api.get_config())
    assert read_config()["password"] == "legacy-secret"
    monkeypatch.setattr(config_helpers, "save_config", save_config)

    assert api.save_settings({"password": ""})["ok"] is True
    assert "password" not in read_config()
    assert api.get_config()["credential_warning"] is False


def test_forget_removes_saved_password_and_invalidates_session(make_api, isolated_credentials):
    isolated_credentials.update({"alice": "alice-secret", "bob": "bob-secret"})
    api = make_api()
    api._jwt_token = "alice-session"

    result = api.clear_credentials()

    assert result["ok"] is True
    assert isolated_credentials == {"bob": "bob-secret"}
    assert api._get_login_credentials() == ("alice", "")
    assert api.get_config()["has_password"] is False
    assert api._jwt_token is None
    assert read_config()["username"] == "alice"
    assert "password" not in read_config()


def test_forget_failure_clears_session_but_reports_that_saved_secret_may_remain(make_api, isolated_credentials, monkeypatch, caplog):
    isolated_credentials["alice"] = "alice-secret"
    api = make_api()
    api._jwt_token = "alice-session"
    monkeypatch.setattr(credentials, "delete_password", fail_storage)

    result = api.clear_credentials()

    assert result["ok"] is False
    assert isolated_credentials["alice"] == "alice-secret"
    assert api._get_login_credentials() == ("alice", "")
    assert api._jwt_token is None
    assert api.get_config()["has_password"] is False
    assert "may remain" in result["message"]
    assert "backend-error-with-secret-password" not in json.dumps(result) + caplog.text


def test_forget_can_explicitly_remove_unmigrated_legacy_password(make_api, monkeypatch):
    monkeypatch.setattr(credentials, "set_password", fail_storage)
    api = make_api({"password": "legacy-secret"})
    assert api._legacy_migration_pending is True

    result = api.clear_credentials()

    assert result["ok"] is True
    assert api._legacy_migration_pending is False
    assert api._get_login_credentials() == ("alice", "")
    assert "password" not in read_config()


def test_forget_failure_still_removes_legacy_plaintext_copy(make_api, monkeypatch):
    monkeypatch.setattr(credentials, "set_password", fail_storage)
    monkeypatch.setattr(credentials, "delete_password", fail_storage)
    api = make_api({"password": "legacy-secret"})

    result = api.clear_credentials()

    assert result["ok"] is False
    assert api._get_login_credentials() == ("alice", "")
    assert api._legacy_migration_pending is False
    assert "password" not in read_config()


def test_forget_file_failure_clears_session_and_reports_incomplete_removal(make_api, monkeypatch):
    monkeypatch.setattr(credentials, "set_password", fail_storage)
    api = make_api({"password": "legacy-secret"})
    monkeypatch.setattr(config_helpers, "save_config", MagicMock(side_effect=OSError("file-error-secret")))

    result = api.clear_credentials()

    assert result["ok"] is False
    assert api._get_login_credentials() == ("alice", "")
    assert api.get_config()["credential_warning"] is True
    assert "may remain" in result["message"]
    assert "file-error-secret" not in json.dumps(result)
    assert read_config()["password"] == "legacy-secret"


def test_unexpected_startup_store_failure_always_releases_waiters(make_api, monkeypatch, caplog):
    monkeypatch.setattr(credentials, "get_password", MagicMock(side_effect=RuntimeError("unexpected-secret")))

    api = make_api()

    assert api._credentials_loaded_event.is_set()
    assert api._get_login_credentials() == ("alice", "")
    assert api.get_config()["has_password"] is False
    assert api.get_config()["credential_storage"] == "unavailable"
    assert "unexpected-secret" not in caplog.text + json.dumps(api.get_config())


@pytest.mark.parametrize("username,password", [(None, "secret"), ("alice", None), ("", "secret"), ("alice", "")])
def test_invalid_login_fields_do_not_replace_saved_credentials(make_api, isolated_credentials, username, password):
    isolated_credentials["alice"] = "alice-secret"
    api = make_api()

    assert api.save_credentials(username, password)["ok"] is False
    assert isolated_credentials == {"alice": "alice-secret"}
    assert api._get_login_credentials() == ("alice", "alice-secret")


@pytest.mark.parametrize("operation", [
    lambda api: api.save_credentials("alice", "new-secret"),
    lambda api: api.save_settings({"username": "bob", "password": "bob-secret"}),
    lambda api: api.clear_credentials(),
])
def test_active_fetch_prevents_account_and_password_changes(make_api, isolated_credentials, operation):
    isolated_credentials["alice"] = "alice-secret"
    api = make_api()
    api._fetch_in_progress = True
    api._jwt_token = "active-session"

    assert operation(api)["ok"] is False
    assert api._get_login_credentials() == ("alice", "alice-secret")
    assert api._jwt_token == "active-session"
    assert isolated_credentials == {"alice": "alice-secret"}


@pytest.mark.parametrize("operation", ["forget", "switch"])
@pytest.mark.parametrize("stage", ["login", "scores", "expired_login"])
def test_local_sync_discards_inflight_results_after_credentials_change(
    make_api, isolated_credentials, monkeypatch, tmp_path, operation, stage,
):
    """A delayed login or API result must not restore a forgotten account."""
    import kovaaks.api as remote_api
    import kovaaks.data_processing as data_processing
    import requests

    isolated_credentials["alice"] = "alice-secret"
    api = make_api()
    api._scenario_info = {"one": {"name": "Scenario A", "entries": 1000}}
    filename = "Scenario A - Challenge - 2026.09.30-12.00.00 Stats.csv"
    (tmp_path / filename).write_text("Score:,100\n", encoding="utf-8")
    monkeypatch.setattr(kovaaks_web.time, "sleep", lambda *args: None)
    monkeypatch.setattr(data_processing, "parse_leaderboard_entries", lambda *args: (
        {"rank": 10, "score": 1000}, [{"friend": "alice-friend", "score": 1001}],
    ))

    def change_credentials():
        if operation == "forget":
            assert api.clear_credentials()["ok"] is True
        else:
            assert api.save_credentials("bob", "bob-secret")["ok"] is True

    def login(username, password):
        assert (username, password) == ("alice", "alice-secret")
        change_credentials()
        return "late-alice-session"

    def get_scores(*args, **kwargs):
        if stage == "expired_login":
            response = requests.Response()
            response.status_code = 401
            raise requests.HTTPError(response=response)
        change_credentials()
        return {"stale": "alice-data"}

    login_mock = MagicMock(side_effect=login)
    scores_mock = MagicMock(side_effect=get_scores)
    monkeypatch.setattr(remote_api, "kovaaks_login", login_mock)
    monkeypatch.setattr(remote_api, "kovaaks_get_friends_scores", scores_mock)
    if stage != "login":
        api._jwt_token = "old-alice-session"

    api._handle_new_stats_files(str(tmp_path), [filename])

    assert api._get_login_credentials() == (("alice", "") if operation == "forget" else ("bob", "bob-secret"))
    assert api._jwt_token is None
    assert api._user_by_lid == {}
    assert api._friends_by_lid == {}
    assert api._scores_cache["scores"] == {}
    if stage == "login":
        login_mock.assert_called_once()
        scores_mock.assert_not_called()
    elif stage == "scores":
        login_mock.assert_not_called()
        scores_mock.assert_called_once()
    else:
        login_mock.assert_called_once()
        scores_mock.assert_called_once()
