"""The tracker consumes release assets and falls back safely on download errors."""

import gzip
import json
from types import SimpleNamespace
from unittest.mock import Mock, call

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
    request.assert_called_once_with("get", f"{RELEASE_BASE}/{filename}", timeout=30)
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


def test_missing_release_uses_direct_api_and_completes_refresh(downloads, monkeypatch):
    request, _, _ = downloads
    request.return_value = response(status=404)
    api = Mock(return_value=SCENARIOS)
    save_cache = Mock()
    monkeypatch.setattr(fetch_worker, "fetch_all_scenarios", api)
    monkeypatch.setattr(fetch_worker, "save_scores_cache", save_cache)
    app = Mock()
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
    app._cfg = {"min_entries": 10}
    app._scores_cache = {"scenarios": SCENARIOS, "scores": {}, "entry_history": {}}
    app._fetch_cancelled = False

    fetch_worker.run_fetch_all(app, "test-user", "")

    assert app._scores_cache["scenarios"] == SCENARIOS
    save_cache.assert_not_called()
    app._update_status.assert_called_with("Error: No scenarios available; keeping the previous cache")
    assert app._fetch_in_progress is False
