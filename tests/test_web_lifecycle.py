"""Regression coverage for startup, filtering, and live local statistics."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import kovaaks.cache as cache
import kovaaks.config_helpers as config_helpers
import kovaaks.logging_helpers as logging_helpers
import kovaaks_web


class SyncThread:
    """Run finite background tasks immediately; polling is mocked separately."""

    def __init__(self, target, args=(), kwargs=None, daemon=True):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def make_api(monkeypatch, stats_dir):
    monkeypatch.setattr(kovaaks_web, "load_config", lambda: {"stats_dir": str(stats_dir)})
    monkeypatch.setattr(kovaaks_web, "load_scores_cache", lambda: {
        "scenarios": [{"leaderboardId": "one", "scenarioName": "Scenario A", "counts": {"entries": 1500}}],
        "scores": {"one": {"user": {"rank": 10, "score": 100, "date": "2026-09-01"}}},
    })
    return kovaaks_web.KovaaksAPI()


def write_run(directory, score=120):
    path = directory / "Scenario A - Challenge - 2026.09.30-12.00.00 Stats.csv"
    path.write_text(f"Score:,{score}\n", encoding="utf-8")
    return path


def test_test_paths_are_isolated(tmp_path):
    """Collection-time logging and default stats must also avoid production files."""
    assert cache.SCORES_CACHE == str(tmp_path / "scores_cache.json.gz")
    assert config_helpers.CONFIG_PATH == str(tmp_path / "config.json")
    assert config_helpers.get_default_stats_dir() == str(tmp_path / "stats")
    assert "kovaaks-tests-" in logging_helpers.LOG_FILE


@pytest.mark.parametrize("stage", ["load", "local_stats", "save"])
def test_startup_failure_releases_waiters(monkeypatch, tmp_path, stage):
    api = make_api(monkeypatch, tmp_path)
    api._cache_loaded_event.clear()
    api._update_status = MagicMock()
    api._update_progress = MagicMock()
    failure = MagicMock(side_effect=ValueError("injected startup failure"))
    if stage == "load":
        monkeypatch.setattr(api, "_load_cache_and_populate", failure)
    elif stage == "local_stats":
        monkeypatch.setattr(api, "_refresh_local_stats", failure)
    else:
        api._scores_cache["_dirty"] = True
        monkeypatch.setattr(kovaaks_web, "save_scores_cache", failure)

    api._initial_cache_load()

    assert api._cache_loaded_event.is_set()
    failure.assert_called_once()
    expected_status = "Loaded memory cache" if stage == "save" else "Could not load cached data"
    assert expected_status in api._update_status.call_args.args[0]
    if stage == "save":
        assert api._scores_cache["_dirty"] is True
    api._update_progress.assert_called_once_with(1, 1)
    # API calls complete with an empty result when the same failure persists.
    if stage in ("load", "local_stats"):
        assert api.get_data(1000)["rows"] == []


def test_startup_notification_failure_releases_waiters(monkeypatch, tmp_path):
    api = make_api(monkeypatch, tmp_path)
    api._cache_loaded_event.clear()
    api.window = MagicMock()
    api.window.evaluate_js.side_effect = RuntimeError("window not ready")

    api._initial_cache_load()

    assert api._cache_loaded_event.is_set()
    assert api.window.evaluate_js.call_count == 2
    assert len(api.get_data(1000)["rows"]) == 1


def test_startup_releases_readiness_before_cache_compression(monkeypatch, tmp_path):
    api = make_api(monkeypatch, tmp_path)
    api._cache_loaded_event.clear()
    api._scores_cache["_dirty"] = True
    ready_during_save = []
    monkeypatch.setattr(kovaaks_web, "save_scores_cache", lambda _: (
        ready_during_save.append(api._cache_loaded_event.is_set())
    ))

    api._initial_cache_load()

    assert ready_during_save == [True]


def test_startup_builds_rows_only_for_the_browser_request(monkeypatch, tmp_path):
    api = make_api(monkeypatch, tmp_path)
    api._cache_loaded_event.clear()
    api.window = MagicMock()
    rebuild = MagicMock(wraps=api._rebuild_data)
    monkeypatch.setattr(api, "_rebuild_data", rebuild)

    api._initial_cache_load()

    rebuild.assert_not_called()
    assert not any("fetchData" in call.args[0] for call in api.window.evaluate_js.call_args_list)
    result = api.get_data(1000)
    rebuild.assert_called_once()
    assert len(result["rows"]) == 1
    assert result["global_stats"]["points"] == 1490


def test_first_table_request_reuses_startup_local_parsing(monkeypatch, tmp_path):
    write_run(tmp_path)
    parse = MagicMock(wraps=kovaaks_web._get_local_stats)
    monkeypatch.setattr(kovaaks_web, "_get_local_stats", parse)
    api = make_api(monkeypatch, tmp_path)

    result = api.get_data(1000)

    parse.assert_called_once()
    assert result["rows"][0][result["columns"].index("Local Runs")] == "1"


def test_async_save_failure_is_retried_on_next_data_refresh(monkeypatch, tmp_path):
    api = make_api(monkeypatch, tmp_path)
    api._scores_cache.pop("_dirty", None)
    api._cache_writer = cache.CacheWriter(api._cache_snapshot, save=api._persist_cache_snapshot)
    save = MagicMock(side_effect=[False, True])
    monkeypatch.setattr(kovaaks_web, "save_scores_cache", save)

    api._queue_cache_save()
    # Wait directly on the writer: the asynchronous save callback must mark
    # dirty without relying on the app's explicit flush helper or shutdown.
    assert api._cache_writer.flush() is False
    assert api._scores_cache["_dirty"] is True
    api.get_data(1000)

    assert api._cache_writer.flush() is True
    assert save.call_count == 2
    assert "_dirty" not in api._scores_cache


def test_startup_bounds_existing_history_outside_filter(monkeypatch, tmp_path):
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    points = {(now - datetime.timedelta(hours=i)).isoformat(): 10 for i in range(200)}
    monkeypatch.setattr(kovaaks_web, "load_config", lambda: {"stats_dir": str(tmp_path)})
    monkeypatch.setattr(kovaaks_web, "load_scores_cache", lambda: {
        "entry_history": {"filtered-out": dict(points)}, "scenarios": [], "scores": {},
    })

    api = kovaaks_web.KovaaksAPI()

    assert set(api._scores_cache["entry_history"]["filtered-out"]) == set(sorted(points)[-168:])
    assert api._scores_cache["_dirty"] is True


def test_filter_clears_and_restores_rows(monkeypatch, tmp_path):
    api = make_api(monkeypatch, tmp_path)
    assert len(api.get_data(1000)["rows"]) == 1

    assert api.get_data(2000)["rows"] == []
    assert api._scenario_info == {}
    assert api._user_by_lid == {}
    assert api._friends_by_lid == {}
    assert api._global_points_sum == 0

    assert len(api.get_data(1000)["rows"]) == 1
    api._scores_cache["scenarios"] = []
    assert api.get_data(1000)["rows"] == []


@pytest.mark.parametrize("minimum", [None, "", "invalid"])
def test_invalid_entry_threshold_uses_default(monkeypatch, tmp_path, minimum):
    api = make_api(monkeypatch, tmp_path)
    assert len(api.get_data(minimum)["rows"]) == 1


def test_watcher_run_is_parsed_once_and_persisted(monkeypatch, tmp_path):
    api = make_api(monkeypatch, tmp_path)
    observer = MagicMock()
    monkeypatch.setattr("watchdog.observers.Observer", lambda: observer)
    monkeypatch.setattr(kovaaks_web.threading, "Thread", SyncThread)
    monkeypatch.setattr(kovaaks_web.time, "sleep", lambda _: None)
    api._start_file_watcher()
    handler = observer.schedule.call_args.args[0]
    path = write_run(tmp_path)
    event = SimpleNamespace(is_directory=False, src_path=str(path))

    handler.on_created(event)

    assert path.name in api._known_stat_files
    assert path.name not in api._scores_cache["known_stat_files"]
    data = api.get_data(1000)
    assert data["rows"][0][data["columns"].index("Local Runs")] == "1"
    assert api._scores_cache["local_stats"]["Scenario A"]["recent_scores"][0][1] == 120
    assert cache.load_scores_cache()["local_stats"]["Scenario A"]["count"] == 1

    handler.on_modified(event)
    api.get_data(1000)
    assert api._scores_cache["local_stats"]["Scenario A"]["count"] == 1


def test_initial_scan_leaves_new_runs_for_parser(monkeypatch, tmp_path):
    api = make_api(monkeypatch, tmp_path)
    path = write_run(tmp_path)
    monkeypatch.setattr(api, "_start_file_watcher", lambda: None)
    monkeypatch.setattr(api, "_handle_new_stats_files", MagicMock())
    monkeypatch.setattr(kovaaks_web.threading, "Thread", SyncThread)

    api._start_stats_polling()

    assert path.name in api._known_stat_files
    assert path.name not in api._scores_cache["known_stat_files"]
    api.get_data(1000)
    assert api._scores_cache["local_stats"]["Scenario A"]["count"] == 1


def test_watcher_retries_incomplete_run_without_repeating_autoplay(monkeypatch, tmp_path):
    api = make_api(monkeypatch, tmp_path)
    observer = MagicMock()
    monkeypatch.setattr("watchdog.observers.Observer", lambda: observer)
    monkeypatch.setattr(kovaaks_web.threading, "Thread", SyncThread)
    monkeypatch.setattr(kovaaks_web.time, "sleep", lambda _: None)
    api.window = MagicMock()

    def evaluate_js(script):
        if "window.fetchData" in script:
            api.get_data(1000)

    api.window.evaluate_js.side_effect = evaluate_js
    api._start_file_watcher()
    handler = observer.schedule.call_args.args[0]
    path = tmp_path / "Scenario A - Challenge - 2026.09.30-12.00.00 Stats.csv"
    path.write_text("", encoding="utf-8")
    event = SimpleNamespace(is_directory=False, src_path=str(path))
    handler.on_created(event)
    assert path.name not in api._scores_cache["known_stat_files"]
    assert api._scores_cache["local_stats"] == {}

    write_run(tmp_path)
    handler.on_modified(event)
    handler.on_modified(event)

    assert api._scores_cache["local_stats"]["Scenario A"]["count"] == 1
    assert path.name in api._scores_cache["known_stat_files"]
    autoplay_calls = [call for call in api.window.evaluate_js.call_args_list
                      if "onLocalScoreDetected" in call.args[0]]
    assert len(autoplay_calls) == 1


def test_fallback_poll_leaves_new_runs_for_parser(monkeypatch, tmp_path):
    api = make_api(monkeypatch, tmp_path)
    path = write_run(tmp_path)
    monkeypatch.setattr(api, "_handle_new_stats_files", MagicMock())
    monkeypatch.setattr(kovaaks_web.threading, "Thread", SyncThread)

    class StopPolling(BaseException):
        """Stop after one iteration without being swallowed by error handling."""

    with monkeypatch.context() as polling:
        polling.setattr(kovaaks_web.os, "stat", MagicMock(side_effect=[
            SimpleNamespace(st_mtime=0), SimpleNamespace(st_mtime=1),
        ]))
        polling.setattr(kovaaks_web.os.path, "exists", lambda _: True)
        polling.setattr(api._shutdown_event, "wait", MagicMock(side_effect=[None, StopPolling]))
        with pytest.raises(StopPolling):
            api._poll_stats_loop()

    assert path.name in api._known_stat_files
    assert path.name not in api._scores_cache["known_stat_files"]
    api.get_data(1000)
    assert api._scores_cache["local_stats"]["Scenario A"]["count"] == 1


def test_fallback_poll_detects_pending_file_completion(monkeypatch, tmp_path):
    api = make_api(monkeypatch, tmp_path)
    path = tmp_path / "Scenario A - Challenge - 2026.09.30-12.00.00 Stats.csv"
    path.write_text("", encoding="utf-8")
    api.window = MagicMock()
    monkeypatch.setattr(api, "_handle_new_stats_files", MagicMock(
        side_effect=lambda *_: setattr(api, "_local_stats_dirty", False)))
    monkeypatch.setattr(kovaaks_web.threading, "Thread", SyncThread)

    class StopPolling(BaseException):
        """Stop the otherwise infinite poll after completing the test run."""

    directory_mtimes = iter([0, 1, 1])
    original_stat = kovaaks_web.os.stat
    sleeps = 0

    def stat(path_to_check, *args, **kwargs):
        if str(path_to_check) == str(tmp_path):
            return SimpleNamespace(st_mtime=next(directory_mtimes))
        return original_stat(path_to_check, *args, **kwargs)

    def sleep(_):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            write_run(tmp_path)
        elif sleeps == 3:
            raise StopPolling

    with monkeypatch.context() as polling:
        polling.setattr(kovaaks_web.os, "stat", stat)
        polling.setattr(kovaaks_web.os.path, "exists", lambda _: True)
        polling.setattr(api._shutdown_event, "wait", sleep)
        with pytest.raises(StopPolling):
            api._poll_stats_loop()

    api._handle_new_stats_files.assert_called_once()
    api.window.evaluate_js.assert_called_once_with("if(window.fetchData) window.fetchData()")
    assert api._local_stats_dirty
    api.get_data(1000)
    assert api._scores_cache["local_stats"]["Scenario A"]["count"] == 1


def test_switching_stats_directory_discards_old_parsed_stats(monkeypatch, tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    write_run(old, score=100)
    new_run = write_run(new, score=200)
    api = make_api(monkeypatch, old)
    assert api._scores_cache["local_stats"]["Scenario A"]["recent_scores"][0][1] == 100
    monkeypatch.setattr(api, "_start_file_watcher", lambda: None)
    monkeypatch.setattr(kovaaks_web.threading, "Thread", SyncThread)

    api.save_settings({"stats_dir": str(new)})

    assert new_run.name not in api._scores_cache["known_stat_files"]
    assert api._scores_cache["local_stats"] == {}
    api.get_data(1000)
    local = api._scores_cache["local_stats"]["Scenario A"]
    assert local["count"] == 1
    assert local["recent_scores"][0][1] == 200


def test_corrupt_cache_is_not_saved_by_local_stats_updates(monkeypatch, tmp_path):
    api = make_api(monkeypatch, tmp_path)
    api._cache_corrupted = True
    write_run(tmp_path)
    api._local_stats_dirty = True
    save = MagicMock()
    monkeypatch.setattr(kovaaks_web, "save_scores_cache", save)
    monkeypatch.setattr(kovaaks_web.threading, "Thread", SyncThread)

    api.get_data(1000)

    save.assert_not_called()


def test_concurrent_refreshes_share_one_parse_and_correct_totals(monkeypatch, tmp_path):
    """Overlapping JS requests must not double-count runs or global points."""
    api = make_api(monkeypatch, tmp_path)
    write_run(tmp_path)
    api._invalidate_local_stats()
    entered = threading.Event()
    contended = threading.Event()
    release = threading.Event()

    class RecordingRLock:
        def __init__(self):
            self.lock = threading.RLock()

        def __enter__(self):
            if not self.lock.acquire(blocking=False):
                contended.set()
                self.lock.acquire()
            return self

        def __exit__(self, *_):
            self.lock.release()

    original_parse = kovaaks_web._get_local_stats

    def parse(*args):
        entered.set()
        assert release.wait(timeout=3), "Test did not release the stats parser"
        return original_parse(*args)

    api._data_lock = RecordingRLock()
    parser = MagicMock(side_effect=parse)
    monkeypatch.setattr(kovaaks_web, "_get_local_stats", parser)
    monkeypatch.setattr(kovaaks_web, "save_scores_cache", MagicMock())
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(api.get_data, 1000)
        try:
            assert entered.wait(timeout=3), "First refresh never reached the parser"
            second = executor.submit(api.get_data, 1000)
            assert contended.wait(timeout=3), "Second refresh did not wait for the first"
        finally:
            release.set()
        results = [first.result(timeout=3), second.result(timeout=3)]

    parser.assert_called_once()
    assert api._scores_cache["local_stats"]["Scenario A"]["count"] == 1
    for result in results:
        assert result["rows"][0][result["columns"].index("Local Runs")] == "1"
        assert result["global_stats"]["points"] == 1490
