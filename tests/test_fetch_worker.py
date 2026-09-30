import os
import sys
import copy
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovaaks.fetch_worker import run_fetch_all

class TestFetchWorkerWorkItems:
    @patch("concurrent.futures.as_completed")
    @patch("kovaaks.fetch_worker.fetch_gzip_json_from_github")
    @patch("kovaaks.fetch_worker.kovaaks_login")
    @patch("concurrent.futures.ThreadPoolExecutor")
    @patch("kovaaks.fetch_worker.save_scores_cache")
    def test_run_fetch_all_work_items_selection(self, mock_save, mock_executor, mock_login, mock_github, mock_as_completed):
        # 1. Setup mock app
        app = MagicMock()
        app._cfg = {"min_entries": 10}
        
        # Initial scores_cache state:
        # lid-1: not in scores_data at all -> must fetch
        # lid-2: in scores_data (unplayed) but in newly_played_scenarios -> must fetch
        # lid-3: in scores_data (unplayed) and has local runs but no user score -> must fetch
        # lid-4: in scores_data (unplayed), has NO local runs, not newly played -> do NOT fetch
        # lid-5: in scores_data (played), has local runs AND cached user score, not newly played -> do NOT fetch
        app._scores_cache = {
            "scenarios": [],
            "entry_history": {},
            "scores": {
                "lid-2": {},
                "lid-3": {},
                "lid-4": {},
                "lid-5": {"user": {"score": 100, "rank": 5}}
            },
            "newly_played_scenarios": ["Scen 2"],
            "local_stats": {
                "Scen 3": {"count": 3},
                "Scen 4": {"count": 0},
                "Scen 5": {"count": 5}
            }
        }
        
        # Return scenarios list from github mock
        mock_github.side_effect = [
            [
                {"leaderboardId": "lid-1", "scenarioName": "Scen 1", "counts": {"entries": 100}},
                {"leaderboardId": "lid-2", "scenarioName": "Scen 2", "counts": {"entries": 100}},
                {"leaderboardId": "lid-3", "scenarioName": "Scen 3", "counts": {"entries": 100}},
                {"leaderboardId": "lid-4", "scenarioName": "Scen 4", "counts": {"entries": 100}},
                {"leaderboardId": "lid-5", "scenarioName": "Scen 5", "counts": {"entries": 100}},
            ],
            None # scenarios_history.json.gz
        ]
        
        # Mock login to succeed
        mock_login.return_value = "fake-jwt-token"
        
        # Capture the work items sent to the executor
        captured_work_items = []
        
        # Mock as_completed to just yield the mock futures
        mock_as_completed.side_effect = lambda futures: iter(futures)

        def mock_submit(fn, lid, session):
            captured_work_items.append(lid)
            future = MagicMock()
            future.result.return_value = None
            return future

        mock_instance = MagicMock()
        mock_instance.submit.side_effect = mock_submit
        mock_executor.return_value = mock_instance
        
        # Run fetch
        run_fetch_all(app, "test_user", "test_pass")
        
        # Verify work items
        assert "lid-1" in captured_work_items
        assert "lid-2" in captured_work_items
        assert "lid-3" in captured_work_items
        assert "lid-4" in captured_work_items
        assert "lid-5" in captured_work_items
        
        # "newly_played_scenarios" should have been popped/removed from scores_cache
        assert "newly_played_scenarios" not in app._scores_cache

    @patch("kovaaks.fetch_worker.fetch_gzip_json_from_github")
    @patch("kovaaks.fetch_worker.kovaaks_login")
    @patch("concurrent.futures.ThreadPoolExecutor")
    def test_run_fetch_all_respects_cancellation(self, mock_executor, mock_login, mock_github):
        app = MagicMock()
        app._cfg = {"min_entries": 10}
        app._scores_cache = {
            "scenarios": [],
            "entry_history": {},
            "scores": {}
        }
        
        # Set cancellation flag to True
        app._fetch_cancelled = True
        
        # Run fetch
        run_fetch_all(app, "test_user", "test_pass")
        
        # Verify early return: github, login, and executor should NOT be called/created
        mock_github.assert_not_called()
        mock_login.assert_not_called()
        mock_executor.assert_not_called()
        
        # Verify the cancellation handler was called on app
        app._rebuild_data_and_cancelled.assert_called_once()


@pytest.mark.parametrize("blocked_stage", ["request", "parse"])
def test_cancelled_fetch_discards_late_worker_result(monkeypatch, blocked_stage):
    """A worker from a cancelled run cannot affect a later fetch's state."""
    from kovaaks import fetch_worker

    app = MagicMock()
    app._cfg = {"min_entries": 10}
    app._fetch_cancelled = False
    app._scores_cache = {
        "scenarios": [],
        "entry_history": {},
        "scores": {"slow": {"user": {"score": 10, "rank": 5}}},
    }
    scenarios = [
        {"leaderboardId": lid, "scenarioName": lid, "counts": {"entries": 100}}
        for lid in ("slow", "fast")
    ]
    entered = threading.Event()
    release = threading.Event()
    futures = {}
    slow_data = [{
        "webappUsername": "test_user", "steamAccountName": "",
        "score": 123, "rank": 1, "attributes": {},
    }]

    def block_worker():
        entered.set()
        assert release.wait(timeout=3), "Test did not release the blocked worker"

    def fetch_scores(token, lid, session, **kwargs):
        if lid == "slow":
            if blocked_stage == "request":
                block_worker()
            return slow_data
        assert entered.wait(timeout=3), "Slow worker never reached its blocking stage"
        app._fetch_cancelled = True
        return []

    original_parse = fetch_worker.parse_leaderboard_entries

    def parse_scores(data, username):
        if data is slow_data and blocked_stage == "parse":
            block_worker()
        return original_parse(data, username)

    class RecordingExecutor(ThreadPoolExecutor):
        def submit(self, fn, lid, session):
            future = super().submit(fn, lid, session)
            futures[lid] = future
            return future

    save = MagicMock()
    monkeypatch.setattr(fetch_worker, "fetch_gzip_json_from_github",
                        MagicMock(side_effect=[scenarios, None]))
    monkeypatch.setattr(fetch_worker, "kovaaks_login", lambda *_, **__: "token")
    monkeypatch.setattr(fetch_worker, "kovaaks_get_friends_scores", fetch_scores)
    monkeypatch.setattr(fetch_worker, "parse_leaderboard_entries", parse_scores)
    monkeypatch.setattr(fetch_worker, "save_scores_cache", save)
    monkeypatch.setattr(fetch_worker.concurrent.futures, "ThreadPoolExecutor", RecordingExecutor)

    try:
        run_fetch_all(app, "test_user", "password")

        app._rebuild_data_and_cancelled.assert_called_once_with(silent=False)
        assert app._fetch_cancelled is False
        assert not futures["slow"].done()
        cache_after_cancellation = copy.deepcopy(app._scores_cache)
        calls_after_cancellation = list(app.method_calls)
        saves_after_cancellation = save.call_count

        # A new fetch can start once the shared cancellation flag resets.
        app._fetch_in_progress = True
        release.set()
        futures["slow"].result(timeout=3)

        assert app._scores_cache == cache_after_cancellation
        assert app._user_by_lid["slow"]["score"] == 10
        assert app.method_calls == calls_after_cancellation
        assert save.call_count == saves_after_cancellation
    finally:
        release.set()
        for future in futures.values():
            if not future.cancelled():
                future.result(timeout=3)


@pytest.mark.parametrize("response, user_score, friend_names", [
    ([], None, []),
    ([{"webappUsername": "test_user", "score": 123, "rank": 1}], 123, []),
    ([{"webappUsername": "new_friend", "score": 456, "rank": 2}], None, ["new_friend"]),
    (None, 10, ["old_friend"]),
], ids=["empty", "user_only", "friends_only", "no_response"])
def test_refresh_removes_only_authoritatively_missing_scores(
        monkeypatch, response, user_score, friend_names):
    """Successful refreshes must replace both persisted and displayed scores."""
    from kovaaks import fetch_worker

    app = MagicMock()
    app._cfg = {"min_entries": 10}
    app._fetch_cancelled = False
    app._scores_cache = {
        "scenarios": [], "entry_history": {},
        "scores": {"lid": {
            "user": {"score": 10, "rank": 5},
            "friends": [{"friend": "old_friend", "score": 20, "rank": 4}],
        }},
    }
    scenarios = [{
        "leaderboardId": "lid", "scenarioName": "Scenario", "counts": {"entries": 100},
    }]
    monkeypatch.setattr(fetch_worker, "fetch_gzip_json_from_github",
                        MagicMock(side_effect=[scenarios, None]))
    monkeypatch.setattr(fetch_worker, "kovaaks_login", lambda *_, **__: "token")
    monkeypatch.setattr(fetch_worker, "kovaaks_get_friends_scores", lambda *_, **__: response)
    monkeypatch.setattr(fetch_worker, "save_scores_cache", MagicMock())

    run_fetch_all(app, "test_user", "password")

    cached = app._scores_cache["scores"]["lid"]
    if user_score is None:
        assert app._user_by_lid == {}
        assert "user" not in cached
    else:
        assert app._user_by_lid["lid"]["score"] == user_score
        assert cached["user"] == app._user_by_lid["lid"]
    if friend_names:
        assert [friend["friend"] for friend in app._friends_by_lid["lid"]] == friend_names
        assert cached["friends"] == app._friends_by_lid["lid"]
    else:
        assert app._friends_by_lid == {}
        assert "friends" not in cached


def test_finalizer_does_not_reset_new_fetch_cancellation():
    """Finish resetting old flags before allowing another fetch to start."""
    class RestartingApp(MagicMock):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if (name == "_fetch_in_progress" and value is False
                    and self.__dict__.get("_restart_on_finish", False)):
                # Simulate a new fetch starting and being cancelled immediately.
                self._fetch_in_progress = True
                self._fetch_cancelled = True

    app = RestartingApp()
    app._fetch_cancelled = True
    app._restart_on_finish = True

    run_fetch_all(app, "test_user", "password")

    assert app._fetch_in_progress is True
    assert app._fetch_cancelled is True


def test_cancellation_finishes_existing_callback_before_next_fetch(monkeypatch):
    """A callback already entered must finish before another fetch can start."""
    from kovaaks import fetch_worker

    app = MagicMock()
    app._cfg = {"min_entries": 10}
    app._fetch_cancelled = False
    app._scores_cache = {"scenarios": [], "entry_history": {}, "scores": {}}
    scenarios = [
        {"leaderboardId": lid, "scenarioName": lid, "counts": {"entries": 100}}
        for lid in ("first", "second")
    ]
    entered = threading.Event()
    release = threading.Event()
    cancel_requested = threading.Event()
    finished = threading.Event()
    futures_seen = []

    def update_status(message):
        if message.startswith("Fetching scores…"):
            entered.set()
            assert release.wait(timeout=3), "Test did not release the UI callback"

    def completed_after_cancel(futures):
        futures_seen.extend(futures)
        assert entered.wait(timeout=3), "Worker never entered its UI callback"
        done, _ = wait(futures, timeout=3, return_when=FIRST_COMPLETED)
        assert done, "No score worker finished"
        app._fetch_cancelled = True
        cancel_requested.set()
        yield next(iter(done))

    def run():
        try:
            run_fetch_all(app, "test_user", "password")
        finally:
            finished.set()

    app._update_status.side_effect = update_status
    monkeypatch.setattr(fetch_worker, "fetch_gzip_json_from_github",
                        MagicMock(side_effect=[scenarios, None]))
    monkeypatch.setattr(fetch_worker, "kovaaks_login", lambda *_, **__: "token")
    monkeypatch.setattr(fetch_worker, "kovaaks_get_friends_scores", lambda *_, **__: [])
    monkeypatch.setattr(fetch_worker, "save_scores_cache", MagicMock())
    monkeypatch.setattr(fetch_worker.concurrent.futures, "as_completed", completed_after_cancel)
    runner = threading.Thread(target=run, daemon=True)

    try:
        runner.start()
        assert cancel_requested.wait(timeout=3), "Fetch never observed cancellation"
        progress_calls = app._update_progress.call_count
        assert not finished.wait(timeout=0.05)
        assert app._fetch_in_progress is True

        release.set()
        assert finished.wait(timeout=3), "Fetch never finished cancellation"
        assert app._update_progress.call_count == progress_calls
        app._rebuild_data_and_cancelled.assert_called_once_with(silent=False)

        app._fetch_in_progress = True
        app._fetch_cancelled = True
        for future in futures_seen:
            future.result(timeout=3)
        assert app._fetch_cancelled is True
    finally:
        release.set()
        runner.join(timeout=3)
        for future in futures_seen:
            if not future.cancelled():
                future.result(timeout=3)
