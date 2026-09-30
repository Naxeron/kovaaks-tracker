"""Deterministic coverage for coalesced, ordered cache persistence."""

from copy import deepcopy
import threading
from unittest.mock import Mock

import pytest

import kovaaks.cache as cache


def test_coalesces_pending_requests_and_snapshots_only_when_ready(monkeypatch):
    """A blocked first write is followed by one fresh, detached snapshot."""
    original_thread = threading.Thread
    workers = []

    def start_thread(**kwargs):
        thread = original_thread(**kwargs)
        workers.append(thread)
        return thread

    monkeypatch.setattr(cache.threading, "Thread", start_thread)
    state_lock = threading.Lock()
    state = {"scores": {"one": 1}}
    snapshots = []
    saved = []
    first_save_started = threading.Event()
    release_first_save = threading.Event()

    def snapshot():
        with state_lock:
            result = deepcopy(state)
        snapshots.append(result)
        return result

    def save(data):
        if not saved:
            first_save_started.set()
            assert release_first_save.wait(5), "First save was never released"
        saved.append(deepcopy(data))
        return cache.save_scores_cache(data)

    writer = cache.CacheWriter(snapshot, save=save)
    try:
        assert writer.request()
        assert first_save_started.wait(5)
        with state_lock:
            state["scores"]["one"] = 2
        writer.request()
        with state_lock:
            state["scores"]["one"] = 3
        writer.request()
        # Queuing more work must not copy the live cache while compression runs.
        assert snapshots == [{"scores": {"one": 1}}]
    finally:
        release_first_save.set()
        for worker in workers:
            worker.join(5)

    assert len(workers) == 1
    assert not workers[0].is_alive()
    assert saved == [{"scores": {"one": 1}}, {"scores": {"one": 3}}]
    assert cache.load_scores_cache() == {"scores": {"one": 3}}


def test_wait_returns_only_after_snapshot_is_persisted():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    result = []

    def save(data):
        started.set()
        assert release.wait(5), "Save was never released"
        return cache.save_scores_cache(data)

    writer = cache.CacheWriter(lambda: {"latest": 42}, save=save)

    def request_and_wait():
        result.append(writer.request(wait=True))
        finished.set()

    waiter = threading.Thread(target=request_and_wait, daemon=True)
    waiter.start()
    try:
        assert started.wait(5)
        assert not finished.is_set()
    finally:
        release.set()
        waiter.join(5)

    assert not waiter.is_alive()
    assert result == [True]
    assert cache.load_scores_cache() == {"latest": 42}


def test_flush_waits_for_existing_request_without_another_snapshot():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    results = []
    snapshot = Mock(return_value={"flush": True})

    def save(data):
        started.set()
        assert release.wait(5), "Save was never released"
        return cache.save_scores_cache(data)

    writer = cache.CacheWriter(snapshot, save=save)
    assert writer.flush() is True
    writer.request()

    def flush():
        results.append(writer.flush())
        finished.set()

    waiter = threading.Thread(target=flush, daemon=True)
    try:
        assert started.wait(5)
        waiter.start()
        assert not finished.is_set()
    finally:
        release.set()
        if waiter.ident is not None:
            waiter.join(5)

    assert not waiter.is_alive()
    assert results == [True]
    snapshot.assert_called_once()
    assert writer.flush() is True
    snapshot.assert_called_once()
    assert cache.load_scores_cache() == {"flush": True}


@pytest.mark.parametrize("failure_stage", ["snapshot", "save", "save_returns_false"])
@pytest.mark.parametrize("synchronous", [False, True])
def test_failure_releases_waiter_and_next_request_retries(failure_stage, synchronous):
    attempts = 0

    def snapshot():
        nonlocal attempts
        attempts += 1
        if attempts == 1 and failure_stage == "snapshot":
            raise RuntimeError("Snapshot unavailable")
        return {"attempt": attempts}

    def save(data):
        if attempts == 1:
            if failure_stage == "save":
                raise OSError("Disk unavailable")
            if failure_stage == "save_returns_false":
                return False
        return cache.save_scores_cache(data)

    writer = cache.CacheWriter(snapshot, save=save, synchronous=synchronous)
    results = []
    waiter = threading.Thread(target=lambda: results.append(writer.request(wait=True)), daemon=True)
    waiter.start()
    waiter.join(5)
    assert not waiter.is_alive(), "Failed persistence left a waiter blocked"
    assert results == [False]
    assert writer.flush() is False
    assert writer.request(wait=True)
    assert cache.load_scores_cache() == {"attempt": 2}


def test_none_snapshot_preserves_existing_cache():
    assert cache.save_scores_cache({"preserve": True})
    save = Mock(wraps=cache.save_scores_cache)
    writer = cache.CacheWriter(lambda: None, save=save, synchronous=True)

    assert writer.request(wait=True) is False

    save.assert_not_called()
    assert cache.load_scores_cache() == {"preserve": True}


def test_synchronous_mode_does_not_create_worker(monkeypatch):
    def unexpected_thread(**kwargs):
        raise AssertionError("Synchronous mode must not create a thread")

    monkeypatch.setattr(cache.threading, "Thread", unexpected_thread)
    writer = cache.CacheWriter(lambda: {"synchronous": True}, synchronous=True)

    assert writer.request()
    assert cache.load_scores_cache() == {"synchronous": True}


def test_inline_thread_double_does_not_deadlock(monkeypatch):
    class InlineThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(cache.threading, "Thread", InlineThread)
    writer = cache.CacheWriter(lambda: {"inline": True})

    assert writer.request(wait=True)
    assert cache.load_scores_cache() == {"inline": True}


def test_thread_start_failure_allows_retry(monkeypatch):
    original_thread = threading.Thread
    thread_factory = Mock(side_effect=RuntimeError("No thread available"))
    monkeypatch.setattr(cache.threading, "Thread", thread_factory)
    writer = cache.CacheWriter(lambda: {"retried": True})

    assert writer.request(wait=True) is False

    monkeypatch.setattr(cache.threading, "Thread", original_thread)
    assert writer.request(wait=True)
    assert cache.load_scores_cache() == {"retried": True}
