"""Bounded refresh work and prompt release of imported-history allocations."""

import threading
import weakref
import datetime
from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest
import requests

from kovaaks import fetch_worker
from kovaaks.history import CompactHistory


class DeferredExecutor:
    """Run one submitted task when as_completed is polled, without threads."""

    def __init__(self, max_workers):
        self.max_workers = max_workers
        self.jobs = {}
        self.submitted = []
        self.peak_pending = 0
        self.cancelled = 0

    def submit(self, function, item, session):
        future = Future()
        self.jobs[future] = (function, item, session)
        self.submitted.append(item)
        self.peak_pending = max(self.peak_pending, len(self.jobs))
        return future

    def complete_one(self, futures):
        # Complete the newest job first, exercising out-of-order replenishment.
        future = next(reversed(self.jobs))
        assert future in futures
        function, item, session = self.jobs.pop(future)
        try:
            future.set_result(function(item, session))
        except BaseException as error:
            future.set_exception(error)
        return iter([future])

    def shutdown(self, wait, cancel_futures):
        assert wait is False
        assert cancel_futures is True
        self.cancelled += sum(future.cancel() for future in self.jobs)
        self.jobs.clear()


@pytest.fixture
def refresh(monkeypatch):
    app = MagicMock()
    app._cfg = {"min_entries": 10}
    app._scores_cache = {"scenarios": [], "scores": {}, "entry_history": {}}
    app._fetch_cancelled = False
    app._data_lock = threading.RLock()
    executor = DeferredExecutor(fetch_worker.API_FETCH_WORKERS)
    monkeypatch.setattr(fetch_worker.concurrent.futures, "ThreadPoolExecutor", lambda **_: executor)
    monkeypatch.setattr(fetch_worker.concurrent.futures, "as_completed", executor.complete_one)
    monkeypatch.setattr(fetch_worker, "kovaaks_login", lambda *_, **__: "token")
    monkeypatch.setattr(fetch_worker, "save_scores_cache", MagicMock())
    return app, executor


def scenarios(count):
    return [{"leaderboardId": str(index), "scenarioName": f"Scenario {index}",
             "counts": {"entries": 100}} for index in range(count)]


@pytest.mark.parametrize("failed_id", [None, "17"])
def test_large_refresh_fetches_every_score_with_bounded_pending_tasks(monkeypatch, refresh, failed_id):
    app, executor = refresh
    data = scenarios(1000)
    requested = []
    monkeypatch.setattr(fetch_worker, "fetch_gzip_json_from_github", MagicMock(side_effect=[data, None]))

    def get_scores(token, lid, session, cancel_check):
        assert cancel_check() is False
        requested.append(lid)
        if lid == failed_id:
            raise requests.ConnectionError("Synthetic failure")
        return []

    monkeypatch.setattr(fetch_worker, "kovaaks_get_friends_scores", get_scores)
    fetch_worker.run_fetch_all(app, "test_user", "password")

    assert len(requested) == len(data)
    assert set(requested) == {item["leaderboardId"] for item in data}
    assert executor.peak_pending == 2 * fetch_worker.API_FETCH_WORKERS
    assert len(app._scores_cache["scores"]) == len(data) - (failed_id is not None)
    app._rebuild_data_and_finish.assert_called_once_with(int(failed_id is not None), silent=False)
    assert app._fetch_in_progress is False


@pytest.mark.parametrize("stop_reason", ["cancel", "expired"])
def test_stopped_refresh_does_not_submit_remaining_scenarios(monkeypatch, refresh, stop_reason):
    app, executor = refresh
    monkeypatch.setattr(fetch_worker, "fetch_gzip_json_from_github",
                        MagicMock(side_effect=[scenarios(1000), None]))
    requests_seen = []

    def stop_request(token, lid, **kwargs):
        requests_seen.append(lid)
        if stop_reason == "cancel":
            app._fetch_cancelled = True
            return []
        response = requests.Response()
        response.status_code = 401
        raise requests.HTTPError(response=response)

    monkeypatch.setattr(fetch_worker, "kovaaks_get_friends_scores", stop_request)
    fetch_worker.run_fetch_all(app, "test_user", "password")

    assert len(requests_seen) == 1
    assert len(executor.submitted) == 2 * fetch_worker.API_FETCH_WORKERS
    assert executor.cancelled == 2 * fetch_worker.API_FETCH_WORKERS - 1
    assert app._scores_cache["scores"] == {}
    assert app._fetch_in_progress is False
    if stop_reason == "cancel":
        app._rebuild_data_and_cancelled.assert_called_once_with(silent=False)
        assert app._fetch_cancelled is False
    else:
        assert app._jwt_token is None
        app._rebuild_data_and_finish.assert_called_once_with(
            silent=False, msg="Session expired — progress saved. Try again.")


def test_downloaded_history_containers_are_released_before_score_requests(monkeypatch, refresh):
    app, _ = refresh
    references = []

    class WeakDict(dict):
        pass

    class WeakList(list):
        pass

    def download(filename, app):
        if filename == "scenarios.json.gz":
            return scenarios(1)
        timestamps = WeakList(["2026-09-30T12:00:00"])
        counts = WeakList([100])
        history = WeakDict({"0": counts})
        payload = WeakDict(timestamps=timestamps, history=history)
        references.extend(weakref.ref(value) for value in (payload, timestamps, history, counts))
        return payload

    def get_scores(*args, **kwargs):
        assert len(references) == 4
        assert all(reference() is None for reference in references)
        assert app._scores_cache["entry_history"]["0"] == {"2026-09-30T12:00:00": 100}
        return []

    monkeypatch.setattr(fetch_worker, "fetch_gzip_json_from_github", download)
    requested = MagicMock(side_effect=get_scores)
    monkeypatch.setattr(fetch_worker, "kovaaks_get_friends_scores", requested)
    fetch_worker.run_fetch_all(app, "test_user", "password")

    requested.assert_called_once()
    # Worker exceptions are collected as fetch errors, so check successful completion too.
    app._rebuild_data_and_finish.assert_called_once_with(0, silent=False)


@pytest.mark.parametrize("cancelled_dataset", ["scenarios.json.gz", "scenarios_history.json.gz"])
def test_cancelled_download_uses_normal_cancellation_finalizer(monkeypatch, refresh, cancelled_dataset):
    app, executor = refresh
    original = {"scenarios": scenarios(1), "scores": {"0": {"user": {"rank": 42}}},
                "entry_history": {"0": CompactHistory({"2020-01-01": 100})}}
    app._scores_cache = original
    from copy import deepcopy
    before = deepcopy(original)

    def download(filename, app):
        if filename == cancelled_dataset:
            raise fetch_worker.RequestCancelled("Fetch cancelled")
        return scenarios(2)

    monkeypatch.setattr(fetch_worker, "fetch_gzip_json_from_github", download)
    fetch_worker.run_fetch_all(app, "test_user", "password")

    assert app._scores_cache == before
    assert executor.submitted == []
    assert app._fetch_in_progress is False
    assert app._fetch_cancelled is False
    app._rebuild_data_and_cancelled.assert_called_once_with(silent=False)
    app._rebuild_data_and_finish.assert_not_called()
    assert not any(str(call.args[0]).startswith("Error:") for call in app._update_status.call_args_list)


def test_imported_compact_history_preserves_existing_and_first_duplicate_samples(monkeypatch, refresh):
    app, _ = refresh
    stamps = [f"2020-01-01T{hour:02}:00:00" for hour in range(4)]
    app._scores_cache["entry_history"] = {"0": CompactHistory({stamps[0]: 777})}
    external = {"timestamps": [stamps[0], stamps[1], stamps[1], stamps[2], stamps[3]],
                "history": {"0": [10, 20, 999, None, 40], "1": [11, 21, 999, None, 41]}}
    monkeypatch.setattr(fetch_worker, "fetch_gzip_json_from_github",
                        MagicMock(side_effect=[scenarios(2), external]))
    monkeypatch.setattr(fetch_worker, "kovaaks_get_friends_scores", lambda *_, **__: [])

    fetch_worker.run_fetch_all(app, "test_user", "password")

    first, second = (app._scores_cache["entry_history"][lid] for lid in ("0", "1"))
    assert isinstance(first, CompactHistory) and isinstance(second, CompactHistory)
    assert first.is_compact and second.is_compact
    assert dict(first) == {stamps[0]: 777, stamps[1]: 20, stamps[3]: 40}
    assert dict(second) == {stamps[0]: 11, stamps[1]: 21, stamps[3]: 41}
    assert first.timestamps is second.timestamps
    assert len(first.packed_counts) == len(second.packed_counts) == 24
    app._rebuild_data_and_finish.assert_called_once_with(0, silent=False)


def test_complete_import_keeps_newest_168_samples_in_compact_storage(monkeypatch, refresh):
    app, _ = refresh
    start = datetime.datetime(2020, 1, 1)
    stamps = [(start + datetime.timedelta(hours=hour)).isoformat() for hour in range(200)]
    external = {"timestamps": stamps, "history": {"0": list(range(200))}}
    monkeypatch.setattr(fetch_worker, "fetch_gzip_json_from_github",
                        MagicMock(side_effect=[scenarios(1), external]))
    monkeypatch.setattr(fetch_worker, "kovaaks_get_friends_scores", lambda *_, **__: [])

    fetch_worker.run_fetch_all(app, "test_user", "password")

    history = app._scores_cache["entry_history"]["0"]
    assert isinstance(history, CompactHistory) and history.is_compact
    assert history.timestamps == tuple(stamps[-168:])
    assert list(history.values()) == list(range(32, 200))
    assert len(history.packed_counts) == 168 * 8


@pytest.mark.parametrize("values,compact,expected", [
    ([None, None], True, {}),
    ([1.5, "legacy"], False, {"2020-01-01T00:00:00": 1.5, "2020-01-01T01:00:00": "legacy"}),
    ([True, 1 << 80], False, {"2020-01-01T00:00:00": True, "2020-01-01T01:00:00": 1 << 80}),
])
def test_empty_or_unusual_imports_preserve_mapping_semantics(monkeypatch, refresh, values, compact, expected):
    app, _ = refresh
    external = {"timestamps": ["2020-01-01T00:00:00", "2020-01-01T01:00:00"],
                "history": {"0": values}}
    monkeypatch.setattr(fetch_worker, "fetch_gzip_json_from_github",
                        MagicMock(side_effect=[scenarios(1), external]))
    monkeypatch.setattr(fetch_worker, "kovaaks_get_friends_scores", lambda *_, **__: [])

    fetch_worker.run_fetch_all(app, "test_user", "password")

    history = app._scores_cache["entry_history"]["0"]
    assert isinstance(history, CompactHistory)
    assert history.is_compact is compact
    assert dict(history) == expected
    assert history.packed_counts == (b"" if compact else None)
    app._rebuild_data_and_finish.assert_called_once_with(0, silent=False)
