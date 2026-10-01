"""The tracker consumes release assets and falls back safely on download errors."""

import gzip
import json
import threading
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call

import pytest
import requests

from kovaaks import fetch_worker


SCENARIOS = [{"leaderboardId": "42", "scenarioName": "Example", "counts": {"entries": 250}}]
HISTORY = {"timestamps": ["2026-09-30T12:00:00"], "history": {"42": [250]}}
RELEASE_BASE = "https://github.com/Naxeron/kovaaks-tracker/releases/download/scenario-data"


def response(data=None, status=200, content=None, headers=None):
    """Build a real response so status errors include their originating response."""
    result = requests.Response()
    result.status_code = status
    result.url = RELEASE_BASE
    result._content = content if content is not None else gzip.compress(json.dumps(data).encode())
    result._content_consumed = True
    result.headers.update(headers or {})
    return result


@pytest.fixture
def downloads(monkeypatch):
    request = Mock()
    sleep = Mock()
    save = Mock()
    monkeypatch.setattr(fetch_worker, "api_request_with_retry", request)
    monkeypatch.setattr(fetch_worker.time, "sleep", sleep)
    monkeypatch.setattr(fetch_worker, "save_config", save)
    return request, sleep, save


@pytest.mark.parametrize("filename,data", [("scenarios.json.gz", SCENARIOS), ("scenarios_history.json.gz", HISTORY)])
def test_downloads_release_asset_and_saves_valid_metadata(downloads, filename, data):
    request, sleep, save = downloads
    request.return_value = response(data, headers={"ETag": '"dataset-version"'})
    app = SimpleNamespace(_cfg={})

    assert fetch_worker.fetch_gzip_json_from_github(filename, app) == data
    request.assert_called_once_with("get", f"{RELEASE_BASE}/{filename}", timeout=30, stream=True, cancel_check=ANY)
    assert app._cfg["last_etags"][filename] == '"dataset-version"'
    save.assert_called_once_with(app._cfg)
    sleep.assert_not_called()


def test_download_retries_temporary_asset_replacement_gap(downloads):
    request, sleep, _ = downloads
    request.side_effect = [response(status=404), response(SCENARIOS)]

    assert fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", SimpleNamespace(_cfg={})) == SCENARIOS
    assert request.call_count == 2
    sleep.assert_called_once_with(0.5)


def test_asset_not_found_retries_are_bounded(downloads):
    request, sleep, save = downloads
    request.return_value = response(status=404)

    assert fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", SimpleNamespace(_cfg={})) is None
    assert request.call_count == 3
    assert sleep.call_args_list == [call(0.5), call(1.0)]
    save.assert_not_called()


@pytest.mark.parametrize("failure", [None, response(status=403), response(status=500), requests.ConnectionError("offline")])
def test_other_download_errors_return_none_without_asset_retry(downloads, failure):
    request, sleep, save = downloads
    if isinstance(failure, Exception):
        request.side_effect = failure
    else:
        request.return_value = failure

    assert fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", SimpleNamespace(_cfg={})) is None
    request.assert_called_once()
    sleep.assert_not_called()
    save.assert_not_called()


@pytest.mark.parametrize("filename,data", [
    ("scenarios.json.gz", {}),
    ("scenarios.json.gz", [42]),
    ("scenarios.json.gz", [{"leaderboardId": "42", "counts": None}]),
    ("scenarios_history.json.gz", {"timestamps": "invalid", "history": {}}),
    ("scenarios_history.json.gz", {"timestamps": [None], "history": {}}),
    ("scenarios_history.json.gz", {"timestamps": ["2026-09-30"], "history": {"42": 250}}),
    ("scenarios_history.json.gz", {"timestamps": ["2026-09-30"], "history": {"42": []}}),
])
def test_invalid_dataset_does_not_replace_download_metadata(downloads, filename, data):
    request, _, save = downloads
    request.return_value = response(data, headers={"ETag": "new"})
    app = SimpleNamespace(_cfg={"last_etags": {filename: "previous"}})

    assert fetch_worker.fetch_gzip_json_from_github(filename, app) is None
    assert app._cfg["last_etags"][filename] == "previous"
    save.assert_not_called()


@pytest.mark.parametrize("content", [b"not gzip", gzip.compress(b"not JSON"), gzip.compress(b"[]")[:-4]])
def test_corrupt_download_returns_none(downloads, content):
    request, _, save = downloads
    request.return_value = response(content=content)

    assert fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", SimpleNamespace(_cfg={})) is None
    save.assert_not_called()


def test_unsupported_filename_is_not_requested(downloads):
    request, _, _ = downloads

    assert fetch_worker.fetch_gzip_json_from_github("other.json.gz", SimpleNamespace(_cfg={})) is None
    request.assert_not_called()


def apply_download(app, filename, data):
    """Mirror a successful install before acknowledging its response metadata."""
    if filename == "scenarios.json.gz":
        app._scores_cache["scenarios"] = data
    else:
        app._scores_cache["entry_history"] = {
            lid: dict(zip(data["timestamps"], counts))
            for lid, counts in data["history"].items()
        }
    fetch_worker.mark_dataset_applied(app, filename)


@pytest.mark.parametrize("filename,data", [("scenarios.json.gz", SCENARIOS), ("scenarios_history.json.gz", HISTORY)])
@pytest.mark.parametrize("validators,expected_headers", [
    ({"ETag": '"v1"'}, {"If-None-Match": '"v1"'}),
    ({"Last-Modified": "Wed, 30 Sep 2026 12:00:00 GMT"},
     {"If-Modified-Since": "Wed, 30 Sep 2026 12:00:00 GMT"}),
    ({"ETag": '"v1"', "Last-Modified": "Wed, 30 Sep 2026 12:00:00 GMT"},
     {"If-None-Match": '"v1"'}),
])
def test_applied_unchanged_dataset_skips_decoding(downloads, monkeypatch, filename, data,
                                                validators, expected_headers):
    request, sleep, save = downloads
    request.side_effect = [response(data, headers=validators), response(status=304, content=b"")]
    app = SimpleNamespace(_cfg={}, _scores_cache={})
    downloaded = fetch_worker.fetch_gzip_json_from_github(filename, app)
    apply_download(app, filename, downloaded)
    decode = Mock(side_effect=AssertionError("Unchanged data must not be decompressed"))
    monkeypatch.setattr(fetch_worker, "read_dataset_response", decode)

    assert fetch_worker.fetch_gzip_json_from_github(filename, app) is fetch_worker.DATASET_UNCHANGED
    request.assert_called_with("get", f"{RELEASE_BASE}/{filename}", timeout=30, stream=True, cancel_check=ANY, headers=expected_headers)
    decode.assert_not_called()
    assert save.call_count == 1
    sleep.assert_not_called()


def test_persisted_validator_without_verified_response_is_not_used(downloads):
    request, _, _ = downloads
    request.return_value = response(SCENARIOS, headers={"ETag": '"new"'})
    app = SimpleNamespace(
        _cfg={"last_etags": {"scenarios.json.gz": '"old"'}},
        _scores_cache={"scenarios": SCENARIOS},
    )

    assert fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", app) == SCENARIOS
    request.assert_called_once_with("get", f"{RELEASE_BASE}/scenarios.json.gz", timeout=30, stream=True, cancel_check=ANY)


@pytest.mark.parametrize("filename,data", [("scenarios.json.gz", SCENARIOS), ("scenarios_history.json.gz", HISTORY)])
def test_unapplied_response_is_downloaded_again(downloads, filename, data):
    request, _, save = downloads
    request.return_value = response(data, headers={"ETag": '"v1"'})
    app = SimpleNamespace(_cfg={}, _scores_cache={})

    # A cancelled fetch or failed merge has downloaded data but never applied it.
    assert fetch_worker.fetch_gzip_json_from_github(filename, app) == data
    assert fetch_worker.fetch_gzip_json_from_github(filename, app) == data

    assert request.call_args_list == [call("get", f"{RELEASE_BASE}/{filename}", timeout=30, stream=True, cancel_check=ANY)] * 2
    assert save.call_count == 1


@pytest.mark.parametrize("replace", ["cache", "scenarios", "history"])
def test_replaced_cache_invalidates_conditional_request(downloads, replace):
    request, _, _ = downloads
    filename, data = (("scenarios_history.json.gz", HISTORY) if replace == "history"
                      else ("scenarios.json.gz", SCENARIOS))
    request.return_value = response(data, headers={"ETag": '"v1"'})
    app = SimpleNamespace(_cfg={}, _scores_cache={})
    downloaded = fetch_worker.fetch_gzip_json_from_github(filename, app)
    apply_download(app, filename, downloaded)
    if replace == "cache":
        app._scores_cache = dict(app._scores_cache)
    elif replace == "scenarios":
        app._scores_cache["scenarios"] = list(downloaded)
    else:
        app._scores_cache["entry_history"] = {}

    assert fetch_worker.fetch_gzip_json_from_github(filename, app) == data
    request.assert_called_with("get", f"{RELEASE_BASE}/{filename}", timeout=30, stream=True, cancel_check=ANY)


def test_scenario_acknowledgment_requires_downloaded_payload_identity(downloads):
    request, _, _ = downloads
    request.return_value = response(SCENARIOS, headers={"ETag": '"v1"'})
    app = SimpleNamespace(_cfg={}, _scores_cache={"scenarios": SCENARIOS})
    fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", app)
    fetch_worker.mark_dataset_applied(app, "scenarios.json.gz")

    assert fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", app) == SCENARIOS
    request.assert_called_with("get", f"{RELEASE_BASE}/scenarios.json.gz", timeout=30, stream=True, cancel_check=ANY)


def test_changed_response_replaces_payload_and_validator_together(downloads):
    request, _, save = downloads
    changed = [{**SCENARIOS[0], "counts": {"entries": 300}}]
    request.side_effect = [response(SCENARIOS, headers={"ETag": '"v1"'}),
                           response(changed, headers={"ETag": '"v2"'}),
                           response(status=304, content=b"")]
    app = SimpleNamespace(_cfg={}, _scores_cache={})
    filename = "scenarios.json.gz"
    apply_download(app, filename, fetch_worker.fetch_gzip_json_from_github(filename, app))

    downloaded = fetch_worker.fetch_gzip_json_from_github(filename, app)
    assert downloaded == changed
    request.assert_called_with("get", f"{RELEASE_BASE}/{filename}", timeout=30, stream=True, cancel_check=ANY,
                               headers={"If-None-Match": '"v1"'})
    apply_download(app, filename, downloaded)

    assert fetch_worker.fetch_gzip_json_from_github(filename, app) is fetch_worker.DATASET_UNCHANGED
    request.assert_called_with("get", f"{RELEASE_BASE}/{filename}", timeout=30, stream=True, cancel_check=ANY,
                               headers={"If-None-Match": '"v2"'})
    assert app._scores_cache["scenarios"] == changed
    assert save.call_count == 2


def test_corrupt_response_does_not_replace_applied_validator(downloads):
    request, _, save = downloads
    request.side_effect = [response(SCENARIOS, headers={"ETag": '"v1"'}),
                           response(content=b"invalid", headers={"ETag": '"broken"'}),
                           response(status=304, content=b"")]
    app = SimpleNamespace(_cfg={}, _scores_cache={})
    filename = "scenarios.json.gz"
    apply_download(app, filename, fetch_worker.fetch_gzip_json_from_github(filename, app))

    assert fetch_worker.fetch_gzip_json_from_github(filename, app) is None
    assert fetch_worker.fetch_gzip_json_from_github(filename, app) is fetch_worker.DATASET_UNCHANGED
    request.assert_called_with("get", f"{RELEASE_BASE}/{filename}", timeout=30, stream=True, cancel_check=ANY,
                               headers={"If-None-Match": '"v1"'})
    assert app._cfg["last_etags"][filename] == '"v1"'
    assert save.call_count == 1


def test_304_without_matching_cache_retries_are_bounded(downloads):
    request, sleep, save = downloads
    request.return_value = response(status=304, content=b"")
    app = SimpleNamespace(_cfg={}, _scores_cache={})

    assert fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", app) is None
    assert request.call_args_list == [call("get", f"{RELEASE_BASE}/scenarios.json.gz", timeout=30, stream=True, cancel_check=ANY)] * 3
    sleep.assert_not_called()
    save.assert_not_called()


def test_cache_replaced_during_304_response_retries_unconditionally(downloads):
    request, _, _ = downloads
    request.return_value = response(SCENARIOS, headers={"ETag": '"v1"'})
    app = SimpleNamespace(_cfg={}, _scores_cache={})
    filename = "scenarios.json.gz"
    apply_download(app, filename, fetch_worker.fetch_gzip_json_from_github(filename, app))

    def replace_cache(*args, **kwargs):
        if "headers" in kwargs:
            app._scores_cache = {}
            return response(status=304, content=b"")
        return response(SCENARIOS, headers={"ETag": '"v1"'})

    request.side_effect = replace_cache
    assert fetch_worker.fetch_gzip_json_from_github(filename, app) == SCENARIOS
    assert request.call_count == 3
    request.assert_called_with("get", f"{RELEASE_BASE}/{filename}", timeout=30, stream=True, cancel_check=ANY)


def test_unchanged_refresh_reuses_scenarios_and_merged_history(downloads, monkeypatch, tmp_path):
    request, _, _ = downloads
    request.side_effect = [
        response(SCENARIOS, headers={"ETag": '"scenarios-v1"'}),
        response(HISTORY, headers={"ETag": '"history-v1"'}),
        response(status=304, content=b""),
        response(status=304, content=b""),
    ]
    from kovaaks import cache
    monkeypatch.setattr(cache, "SCORES_CACHE", str(tmp_path / "scores.json.gz"))
    monkeypatch.setattr(fetch_worker, "save_scores_cache", Mock())
    api = Mock(side_effect=AssertionError("Unchanged scenarios must not trigger API fallback"))
    monkeypatch.setattr(fetch_worker, "fetch_all_scenarios", api)
    app = Mock()
    app._data_lock = threading.RLock()
    app._cfg = {"min_entries": 10}
    app._scores_cache = {"scenarios": [], "scores": {}, "entry_history": {}}
    app._fetch_cancelled = False
    app._legacy_migration_pending = False

    fetch_worker.run_fetch_all(app, "test-user", "")
    scenarios = app._scores_cache["scenarios"]
    history = app._scores_cache["entry_history"]
    decode = Mock(side_effect=AssertionError("An unchanged refresh must not decode assets"))
    monkeypatch.setattr(fetch_worker, "read_dataset_response", decode)
    fetch_worker.run_fetch_all(app, "test-user", "")

    assert app._scores_cache["scenarios"] is scenarios
    assert app._scores_cache["entry_history"] is history
    assert history == {"42": {HISTORY["timestamps"][0]: 250}}
    assert request.call_count == 4
    assert app._rebuild_data_and_finish.call_count == 2
    assert request.call_args_list[-2:] == [
        call("get", f"{RELEASE_BASE}/scenarios.json.gz", timeout=30, stream=True, cancel_check=ANY,
             headers={"If-None-Match": '"scenarios-v1"'}),
        call("get", f"{RELEASE_BASE}/scenarios_history.json.gz", timeout=30, stream=True, cancel_check=ANY,
             headers={"If-None-Match": '"history-v1"'}),
    ]
    api.assert_not_called()
    decode.assert_not_called()


def test_missing_release_uses_direct_api_and_completes_refresh(downloads, monkeypatch):
    request, _, _ = downloads
    request.return_value = response(status=404)
    api = Mock(return_value=SCENARIOS)
    save_cache = Mock()
    monkeypatch.setattr(fetch_worker, "fetch_all_scenarios", api)
    monkeypatch.setattr(fetch_worker, "save_scores_cache", save_cache)
    app = Mock()
    app._data_lock = threading.RLock()
    app._cfg = {"min_entries": 10}
    app._scores_cache = {"scenarios": [], "scores": {}, "entry_history": {}}
    app._fetch_cancelled = False

    fetch_worker.run_fetch_all(app, "test-user", "")

    api.assert_called_once()
    assert app._scores_cache["scenarios"] == SCENARIOS
    save_cache.assert_called_once_with(app._scores_cache)
    app._rebuild_data_and_finish.assert_called_once()
    assert app._fetch_in_progress is False


def test_empty_api_fallback_preserves_previous_scenarios(downloads, monkeypatch):
    request, _, _ = downloads
    request.return_value = response(status=404)
    monkeypatch.setattr(fetch_worker, "fetch_all_scenarios", Mock(return_value=[]))
    save_cache = Mock()
    monkeypatch.setattr(fetch_worker, "save_scores_cache", save_cache)
    app = Mock()
    app._data_lock = threading.RLock()
    app._cfg = {"min_entries": 10}
    app._scores_cache = {"scenarios": SCENARIOS, "scores": {}, "entry_history": {}}
    app._fetch_cancelled = False

    fetch_worker.run_fetch_all(app, "test-user", "")

    assert app._scores_cache["scenarios"] == SCENARIOS
    save_cache.assert_not_called()
    app._update_status.assert_called_with("Error: No scenarios available; keeping the previous cache")
    assert app._fetch_in_progress is False
