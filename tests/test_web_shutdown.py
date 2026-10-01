"""Shutdown regressions without a browser, real network, or production data."""

import gzip
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kovaaks import fetch_worker
import kovaaks_web


class ClosingEvent:
    """Small pywebview event double that invokes registered close handlers."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        return [handler() for handler in self.handlers]


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(kovaaks_web, "load_config", lambda: {"stats_dir": str(tmp_path)})
    monkeypatch.setattr(kovaaks_web, "load_scores_cache", lambda: {})
    instance = kovaaks_web.KovaaksAPI()
    instance._scores_cache.pop("_dirty", None)
    return instance


def make_window():
    return SimpleNamespace(events=SimpleNamespace(closing=ClosingEvent()), evaluate_js=MagicMock())


def test_window_closing_cancels_without_waiting(api, monkeypatch):
    observer = api._watcher_observer = MagicMock()
    fetch_thread = api._fetch_thread = MagicMock()
    stop_entered = threading.Event()
    release_stop = threading.Event()
    observer.stop.side_effect = lambda: (stop_entered.set(), release_stop.wait(timeout=3))
    monkeypatch.setattr(api, "_start_stats_polling", MagicMock())
    window = make_window()
    api.set_window(window)

    try:
        results = window.events.closing.fire()

        assert results and all(result is not False for result in results)
        assert api._shutdown_event.is_set()
        assert api._fetch_cancelled is True
        assert api.window is None
        assert stop_entered.wait(timeout=1)
        observer.join.assert_not_called()
        fetch_thread.join.assert_not_called()
        cleanup = api._watcher_cleanup_thread
        assert cleanup.daemon
        join_cleanup = MagicMock(wraps=cleanup.join)
        monkeypatch.setattr(cleanup, "join", join_cleanup)
        release_stop.set()

        api.shutdown()

        observer.join.assert_called()
        fetch_thread.join.assert_called()
        join_cleanup.assert_called()
        for call, limit in ((observer.join.call_args, 1), (join_cleanup.call_args, 1),
                            (fetch_thread.join.call_args, 2)):
            timeout = call.kwargs.get("timeout", call.args[0] if call.args else None)
            assert timeout is not None and 0 <= timeout <= limit
    finally:
        release_stop.set()


def test_shutdown_suppresses_late_browser_notifications(api):
    window = make_window()
    api.window = window

    api._begin_shutdown()
    api._update_status("Late status")
    api._update_progress(1, 2)
    api._rebuild_data_and_finish()
    api._rebuild_data_and_cancelled()

    window.evaluate_js.assert_not_called()


def test_shutdown_refuses_fetch_even_after_cancel_flag_resets(api, monkeypatch):
    start = MagicMock()
    monkeypatch.setattr(kovaaks_web.threading, "Thread", start)
    api._begin_shutdown()
    api._fetch_cancelled = False

    assert api.fetch_all_stats() is False
    start.assert_not_called()


def test_worker_observes_shutdown_even_after_cancel_flag_resets(api, monkeypatch):
    fetch = MagicMock()
    monkeypatch.setattr(fetch_worker, "fetch_gzip_json_from_github", fetch)
    api._begin_shutdown()
    api._fetch_cancelled = False

    fetch_worker.run_fetch_all(api, "test-user", "test-password")

    fetch.assert_not_called()
    assert api.is_fetch_in_progress() is False


@pytest.mark.parametrize("starter", ["_start_stats_polling", "_start_file_watcher"])
def test_shutdown_prevents_watcher_start(api, monkeypatch, starter):
    start = MagicMock()
    get_directory = MagicMock()
    monkeypatch.setattr(kovaaks_web.threading, "Thread", start)
    monkeypatch.setattr(api, "_get_stats_dir", get_directory)
    api._begin_shutdown()

    getattr(api, starter)()

    get_directory.assert_not_called()
    start.assert_not_called()


def test_shutdown_saves_dirty_cache_and_does_not_rewrite_clean_cache(api, monkeypatch):
    save = MagicMock(return_value=True)
    monkeypatch.setattr(kovaaks_web, "save_scores_cache", save)
    api._scores_cache.update({"scores": {"complete": {"user": {"score": 123}}}, "_dirty": True})

    api.shutdown()

    save.assert_called_once()
    assert save.call_args.args[0]["scores"]["complete"]["user"]["score"] == 123
    api.shutdown()
    save.assert_called_once()


@pytest.mark.parametrize("guard", ["corrupt", "unloaded", "clean"])
def test_shutdown_preserves_unavailable_or_unchanged_cache(api, monkeypatch, guard):
    save = MagicMock(return_value=True)
    monkeypatch.setattr(kovaaks_web, "save_scores_cache", save)
    if guard != "clean":
        api._scores_cache["_dirty"] = True
    if guard == "corrupt":
        api._cache_corrupted = True
    elif guard == "unloaded":
        api._cache_loaded_event.clear()

    api.shutdown()

    save.assert_not_called()


def test_shutdown_flushes_queued_cache_write(api, monkeypatch):
    flush = MagicMock(return_value=True)
    monkeypatch.setattr(api, "_flush_cache_saves", flush)

    api.shutdown()

    flush.assert_called_once()


@pytest.mark.parametrize("close_before_ready", [False, True])
@pytest.mark.parametrize("readiness", ["_cache_loaded_event", "_credentials_loaded_event"])
def test_fetch_waits_for_startup_and_can_stop_while_waiting(api, monkeypatch, close_before_ready, readiness):
    started = threading.Event()
    monkeypatch.setattr(kovaaks_web, "run_fetch_all", lambda *args: started.set())
    ready = getattr(api, readiness)
    ready.clear()

    try:
        assert api.fetch_all_stats() is True
        assert not started.wait(timeout=0.05)
        if close_before_ready:
            api._begin_shutdown()
        else:
            ready.set()
            assert started.wait(timeout=1)
        api._fetch_thread.join(timeout=1)
        assert not api._fetch_thread.is_alive()
        assert started.is_set() is not close_before_ready
    finally:
        ready.set()
        api.shutdown()


@pytest.mark.parametrize("start_fails", [False, True])
def test_main_always_finalizes_shutdown(monkeypatch, start_fails):
    api = MagicMock()
    window = make_window()
    monkeypatch.setattr(kovaaks_web, "KovaaksAPI", lambda: api)
    monkeypatch.setattr(kovaaks_web.webview, "create_window", lambda *args, **kwargs: window)
    start = MagicMock(side_effect=RuntimeError("GUI startup failed") if start_fails else None)
    monkeypatch.setattr(kovaaks_web.webview, "start", start)

    if start_fails:
        with pytest.raises(RuntimeError, match="GUI startup failed"):
            kovaaks_web.main()
    else:
        kovaaks_web.main()

    api.set_window.assert_called_once_with(window)
    api.shutdown.assert_called_once()


def test_process_exits_with_blocked_fetch_and_persists_completed_scores(tmp_path):
    """A stuck request must not keep Python alive after the window closes.

    A subprocess is essential: ordinary thread tests cannot detect Python's
    interpreter-exit joins on executor workers. The blocked request is never
    released, so a process that still waits for it fails the bounded timeout.
    """
    script = textwrap.dedent("""\
        import pathlib
        import sys
        import threading
        import time
        from types import SimpleNamespace

        directory = pathlib.Path(sys.argv[1])
        import kovaaks.cache as cache
        import kovaaks.config_helpers as config
        import kovaaks.credentials as credentials
        import kovaaks.logging_helpers as logging_helpers

        # Redirect all application-owned persistence before importing the GUI
        # module, which installs logging and later starts background loaders.
        cache.SCORES_CACHE = str(directory / "scores_cache.json.gz")
        config.CONFIG_PATH = str(directory / "config.json")
        config.get_default_stats_dir = lambda: str(directory / "stats")
        config.load_config = lambda: {
            "username": "test-user", "min_entries": 10,
            "stats_dir": str(directory / "stats"),
        }
        logging_helpers.LOG_FILE = str(directory / "kovaaks.log")
        credentials.get_password = lambda username: "test-password"
        credentials.set_password = lambda *args: None
        credentials.delete_password = lambda *args: None
        sys.modules["webview"] = SimpleNamespace()

        import kovaaks_web
        from kovaaks import fetch_worker

        scenarios = [
            {"leaderboardId": lid, "scenarioName": lid,
             "counts": {"entries": 100}}
            for lid in ("blocked", "complete")
        ]
        fetch_worker.fetch_gzip_json_from_github = lambda filename, app: (
            scenarios if filename == "scenarios.json.gz" else None
        )
        fetch_worker.kovaaks_login = lambda *args, **kwargs: "test-token"
        blocked = threading.Event()
        never_release = threading.Event()

        def fetch_scores(token, lid, **kwargs):
            if lid == "blocked":
                blocked.set()
                never_release.wait()
            return [{"webappUsername": "test-user", "rank": 2,
                     "score": 123, "attributes": {}}]

        fetch_worker.kovaaks_get_friends_scores = fetch_scores

        class ClosingEvent:
            def __init__(self):
                self.handlers = []
            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self
            def fire(self):
                for handler in self.handlers:
                    assert handler() is not False

        api = kovaaks_web.KovaaksAPI()
        window = SimpleNamespace(
            events=SimpleNamespace(closing=ClosingEvent()),
            evaluate_js=lambda code: None,
        )
        api.set_window(window)
        assert api.fetch_all_stats()
        assert blocked.wait(timeout=5), "The blocking request never started"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with api._data_lock:
                completed = api._scores_cache.get("scores", {}).get("complete")
            if completed:
                break
            time.sleep(0.01)
        assert completed, "The completed request never reached the cache"

        window.events.closing.fire()
        api.shutdown()
        assert cache.load_scores_cache()["scores"]["complete"]["user"]["score"] == 123
        print("shutdown completed", flush=True)
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=12,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "shutdown completed" in result.stdout
    with gzip.open(tmp_path / "scores_cache.json.gz", "rt", encoding="utf-8") as handle:
        saved = json.load(handle)
    assert saved["scores"]["complete"]["user"]["score"] == 123


@pytest.mark.parametrize("blocked_stage", ["history_merge", "history_record", "watcher_stop"])
def test_process_exits_when_callback_or_watcher_never_returns(tmp_path, blocked_stage):
    """Blocked GUI/watcher code must neither retain cache locks nor delay exit."""
    script = textwrap.dedent("""\
        import datetime
        import pathlib
        import sys
        import threading
        from types import SimpleNamespace

        directory = pathlib.Path(sys.argv[1])
        stage = sys.argv[2]
        import kovaaks.cache as cache
        import kovaaks.config_helpers as config
        import kovaaks.credentials as credentials
        import kovaaks.logging_helpers as logging_helpers

        # Isolate paths and credentials before import-time logging and startup.
        cache.SCORES_CACHE = str(directory / "scores_cache.json.gz")
        config.CONFIG_PATH = str(directory / "config.json")
        config.get_default_stats_dir = lambda: str(directory / "stats")
        config.load_config = lambda: {
            "username": "test-user", "min_entries": 10,
            "stats_dir": str(directory / "stats"),
        }
        logging_helpers.LOG_FILE = str(directory / "kovaaks.log")
        credentials.get_password = lambda username: ""
        credentials.set_password = lambda *args: None
        credentials.delete_password = lambda *args: None
        sys.modules["webview"] = SimpleNamespace()

        import kovaaks_web
        from kovaaks import fetch_worker

        # Use the application's normal synchronous test initialization so the
        # original cache checkpoint is complete before injecting a blocked task.
        sys.modules["pytest"] = SimpleNamespace()
        api = kovaaks_web.KovaaksAPI()
        api._scores_cache["scores"] = {"complete": {"user": {"score": 123}}}
        api._scores_cache["entry_history"] = {}
        api._scores_cache.pop("_dirty", None)
        assert cache.save_scores_cache(api._cache_snapshot())
        blocked = threading.Event()
        never_release = threading.Event()
        timestamp = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()

        def block_forever():
            blocked.set()
            never_release.wait()

        if stage == "watcher_stop":
            api._watcher_observer = SimpleNamespace(
                stop=block_forever,
                join=lambda **kwargs: None,
            )
            api._scores_cache["scores"]["complete"]["user"]["score"] = 234
            api._scores_cache["_dirty"] = True
            api._begin_shutdown()
            assert blocked.wait(timeout=3), "Observer stop never started"
        else:
            scenarios = [{"leaderboardId": "sample", "scenarioName": "sample",
                          "counts": {"entries": 100}}]
            history = {"timestamps": [timestamp], "history": {"sample": [99]}}
            fetch_worker.fetch_gzip_json_from_github = lambda filename, app: (
                scenarios if filename == "scenarios.json.gz" else (
                    history if stage == "history_merge" else None
                )
            )

            def progress(current, total):
                accepted = api._scores_cache.get("entry_history", {}).get("sample")
                # The old merge callback and old per-row recording callback
                # ran while owning _data_lock. The final notifications must
                # permit shutdown to snapshot changes even if JS never returns.
                if accepted and (
                    stage == "history_merge" and 0.05 <= current <= 0.10
                    or stage == "history_record" and 0.15 <= current <= 0.22
                ):
                    block_forever()

            api._update_progress = progress
            assert api.fetch_all_stats()
            assert blocked.wait(timeout=3), "History callback never reached its blocking stage"
            api._begin_shutdown()

        api.shutdown()
        saved = cache.load_scores_cache()
        expected_score = 234 if stage == "watcher_stop" else 123
        assert saved["scores"]["complete"]["user"]["score"] == expected_score
        if stage != "watcher_stop":
            expected_count = 99 if stage == "history_merge" else 100
            assert expected_count in saved["entry_history"]["sample"].values()
        print("shutdown completed", flush=True)
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), blocked_stage],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=8,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "shutdown completed" in result.stdout
