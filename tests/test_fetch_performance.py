"""Regression coverage for changed-only, coalesced fetch persistence."""

from concurrent.futures import Future
from copy import deepcopy
import datetime
import threading
from types import SimpleNamespace

import kovaaks.cache as cache
import kovaaks.fetch_worker as worker
from kovaaks_web import KovaaksAPI


class InlineExecutor:
    """Finish bounded network stubs in submission order without worker timing."""

    def __init__(self, max_workers):
        pass

    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, **kwargs):
        pass


class RecordingApp:
    """Use real history/persistence methods with only UI and network removed."""

    _record_history_points = KovaaksAPI._record_history_points
    _cache_snapshot = KovaaksAPI._cache_snapshot

    def __init__(self, count, *, unchanged=False, synchronous=True, save=None):
        self._data_lock = threading.RLock()
        self._cfg = {"min_entries": 10}
        self._fetch_cancelled = False
        self._fetch_in_progress = False
        self._cache_corrupted = False
        self.scenarios = [
            {"leaderboardId": str(i), "scenarioName": f"Scenario {i}",
             "counts": {"entries": 100}}
            for i in range(1, count + 1)
        ]
        stamp = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()
        self._scores_cache = {
            "scenarios": deepcopy(self.scenarios),
            "entry_history": {str(i): {stamp: 100} for i in range(1, count + 1)},
            "scores": {
                str(i): {"user": {"rank": 1, "score": i, "date": ""}}
                for i in range(1, count + 1)
            } if unchanged else {},
        }
        self.queue_calls = 0
        self.flush_calls = 0
        self.cancel_calls = 0
        self.finish_calls = 0
        self.saved = []
        self.final_checkpoint_queued = threading.Event()

        def save_snapshot(snapshot):
            self.saved.append(deepcopy(snapshot))
            return save(snapshot) if save else cache.save_scores_cache(snapshot)

        self._cache_writer = cache.CacheWriter(
            self._cache_snapshot, save=save_snapshot, synchronous=synchronous,
        )

    def _queue_cache_save(self, wait=False):
        self.queue_calls += 1
        result = KovaaksAPI._queue_cache_save(self, wait=wait)
        if self.queue_calls == 3:
            self.final_checkpoint_queued.set()
        return result

    def _flush_cache_saves(self):
        self.flush_calls += 1
        return KovaaksAPI._flush_cache_saves(self)

    def _update_status(self, *args):
        pass

    def _update_progress(self, *args):
        pass

    def _rebuild_data(self):
        pass

    def _rebuild_data_and_finish(self, *args, **kwargs):
        self.finish_calls += 1

    def _rebuild_data_and_cancelled(self, *args, **kwargs):
        self.cancel_calls += 1


def configure_fetch(monkeypatch, app, *, seconds_per_score=0, before_response=None):
    clock = SimpleNamespace(value=0)

    def fetch_scores(token, lid, session, **kwargs):
        clock.value = int(lid) * seconds_per_score
        if before_response:
            before_response(lid)
        return [{"webappUsername": "user", "rank": 1, "score": int(lid), "attributes": {}}]

    def unexpected_direct_save(*args):
        raise AssertionError("GUI fetches must use the shared save queue")

    monkeypatch.setattr(worker, "fetch_gzip_json_from_github", lambda name, _: (
        deepcopy(app.scenarios) if name == "scenarios.json.gz" else worker.DATASET_UNCHANGED
    ))
    monkeypatch.setattr(worker, "kovaaks_login", lambda *_, **__: "token")
    monkeypatch.setattr(worker, "kovaaks_get_friends_scores", fetch_scores)
    monkeypatch.setattr(worker, "save_scores_cache", unexpected_direct_save)
    monkeypatch.setattr(worker.concurrent.futures, "ThreadPoolExecutor", InlineExecutor)
    monkeypatch.setattr(worker, "time", SimpleNamespace(
        time=lambda: clock.value, monotonic=lambda: clock.value,
    ))


def test_fast_fetch_saves_once_at_completion(monkeypatch):
    app = RecordingApp(80)
    configure_fetch(monkeypatch, app)

    worker.run_fetch_all(app, "user", "password")

    assert app.queue_calls == 1
    assert app.flush_calls == 1
    assert len(app.saved) == 1
    assert len(cache.load_scores_cache()["scores"]) == 80
    assert app.finish_calls == 1
    assert not app._fetch_in_progress


def test_unchanged_scores_do_not_schedule_cache_rewrites(monkeypatch):
    app = RecordingApp(80, unchanged=True)
    configure_fetch(monkeypatch, app, seconds_per_score=1)

    worker.run_fetch_all(app, "user", "password")

    assert app.queue_calls == 0
    assert app.saved == []
    assert app.flush_calls == 1
    assert app.finish_calls == 1


def test_score_checkpoints_follow_elapsed_time_and_final_flush(monkeypatch):
    app = RecordingApp(80)
    configure_fetch(monkeypatch, app, seconds_per_score=1)

    worker.run_fetch_all(app, "user", "password")

    assert app.queue_calls == 3
    assert app.flush_calls == 1
    assert [len(snapshot["scores"]) for snapshot in app.saved] == [30, 60, 80]
    assert len(cache.load_scores_cache()["scores"]) == 80


def test_busy_writer_coalesces_checkpoints_and_finalization_waits(monkeypatch):
    first_save_started = threading.Event()
    release_save = threading.Event()

    def save(snapshot):
        if not first_save_started.is_set():
            first_save_started.set()
            assert release_save.wait(5), "Test did not release the cache writer"
        return cache.save_scores_cache(snapshot)

    app = RecordingApp(80, synchronous=False, save=save)

    def before_response(lid):
        if lid == "31":
            assert first_save_started.wait(5), "First checkpoint did not start"

    configure_fetch(monkeypatch, app, seconds_per_score=1, before_response=before_response)
    fetch_thread = threading.Thread(target=worker.run_fetch_all, args=(app, "user", "password"), daemon=True)
    fetch_thread.start()
    try:
        assert app.final_checkpoint_queued.wait(5), "Final checkpoint was never queued"
        assert app._fetch_in_progress
        assert len(app.saved) == 1
    finally:
        release_save.set()
        fetch_thread.join(5)

    assert not fetch_thread.is_alive()
    assert app.queue_calls == 3
    assert app.flush_calls == 1
    assert [len(snapshot["scores"]) for snapshot in app.saved] == [30, 80]
    assert len(cache.load_scores_cache()["scores"]) == 80
    assert not app._fetch_in_progress


def test_cancellation_flushes_only_accepted_updates(monkeypatch):
    app = RecordingApp(3)

    def cancel_second_response(lid):
        if lid == "2":
            app._fetch_cancelled = True

    configure_fetch(monkeypatch, app, before_response=cancel_second_response)

    worker.run_fetch_all(app, "user", "password")

    assert app.cancel_calls == 1
    assert app.queue_calls == 1
    assert app.flush_calls == 1
    assert list(cache.load_scores_cache()["scores"]) == ["1"]
    assert not app._fetch_in_progress
    assert not app._fetch_cancelled


def test_failed_save_is_retried_by_next_unchanged_fetch(monkeypatch):
    attempts = 0

    def save(snapshot):
        nonlocal attempts
        attempts += 1
        return False if attempts == 1 else cache.save_scores_cache(snapshot)

    app = RecordingApp(1, save=save)
    configure_fetch(monkeypatch, app)

    worker.run_fetch_all(app, "user", "password")

    assert app._scores_cache["_dirty"] is True
    assert not app._fetch_in_progress

    worker.run_fetch_all(app, "user", "password")

    assert attempts == 2
    assert app.queue_calls == 2
    assert app.flush_calls == 2
    assert "_dirty" not in app._scores_cache
    assert cache.load_scores_cache()["scores"]["1"]["user"]["score"] == 1
