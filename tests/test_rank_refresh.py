"""Rank lookups share work without publishing another account's results."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import Mock

import pytest

import kovaaks.api
from kovaaks import app as kovaaks_web


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(kovaaks_web, "load_config", lambda: {
        "username": "player", "stats_dir": str(tmp_path),
    })
    monkeypatch.setattr(kovaaks_web, "load_scores_cache", lambda: {"scores": {}})
    monkeypatch.setattr(kovaaks_web, "save_scores_cache", Mock(return_value=True))
    app = kovaaks_web.KovaaksAPI()
    app._global_points_sum = 1000
    return app


@pytest.mark.parametrize("fails", [False, True])
def test_concurrent_cache_misses_share_result_and_release_waiters(api, monkeypatch, fails):
    entered, release, joined = threading.Event(), threading.Event(), threading.Event()

    class Requests(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            if value is not None:
                joined.set()
            return value

    api._next_rank_requests = Requests()

    def lookup(*_):
        entered.set()
        assert release.wait(3), "Test did not release lookup"
        if fails:
            raise RuntimeError("offline")
        return {"next_points": 1500, "user_official_points": 1000}

    lookup = Mock(side_effect=lookup)
    monkeypatch.setattr(kovaaks.api, "get_next_leaderboard_position_points", lookup)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(api.get_next_rank_points)
        try:
            assert entered.wait(3)
            second = executor.submit(api.get_next_rank_points)
            assert joined.wait(3), "Second caller did not share pending lookup"
        finally:
            release.set()
        expected = "Error" if fails else "+500"
        assert first.result(3) == second.result(3) == expected
    lookup.assert_called_once()
    assert api._next_rank_requests == {}
    if fails:
        lookup.side_effect = None
        lookup.return_value = {"next_points": 1500, "user_official_points": 1000}
        assert api.get_next_rank_points() == "+500"
        assert lookup.call_count == 2


@pytest.mark.parametrize("rank_one", [False, True])
def test_stale_cache_returns_immediately_and_only_one_refresh_runs(api, monkeypatch, rank_one):
    api._scores_cache["next_rank"] = {
        "username": "player", "points": 1000 if rank_one else 1200,
        "user_official_points": 1000, "timestamp": time.time() - 7200,
    }
    entered, release = threading.Event(), threading.Event()

    def lookup(*_):
        entered.set()
        assert release.wait(3)
        return {"next_points": 1500, "user_official_points": 1000}

    lookup = Mock(side_effect=lookup)
    monkeypatch.setattr(kovaaks.api, "get_next_leaderboard_position_points", lookup)
    try:
        assert api.get_next_rank_points() == ("Rank 1!" if rank_one else "+200")
        assert entered.wait(3)
        pending = api._next_rank_requests[("player", 0)]
        for _ in range(20):
            assert api.get_next_rank_points() == ("Rank 1!" if rank_one else "+200")
        lookup.assert_called_once()
    finally:
        release.set()
    assert pending.result(3) == "+500"
    assert api.get_next_rank_points() == "+500"
    lookup.assert_called_once()


def test_account_change_discards_pending_rank_and_does_not_save(api, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def lookup(*_):
        entered.set()
        assert release.wait(3)
        return {"next_points": 1500, "user_official_points": 1000}

    monkeypatch.setattr(kovaaks.api, "get_next_leaderboard_position_points", lookup)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(api.get_next_rank_points)
        try:
            assert entered.wait(3)
            with api._credentials_lock:
                api._cfg["username"] = "another-player"
                api._credential_generation += 1
        finally:
            release.set()
        assert pending.result(3) == "N/A"
    assert "next_rank" not in api._scores_cache
    kovaaks_web.save_scores_cache.assert_not_called()


def test_corrupt_cache_remains_protected_on_rank_cache_miss(api, monkeypatch):
    api._cache_corrupted = True
    monkeypatch.setattr(kovaaks.api, "get_next_leaderboard_position_points", lambda *_: {
        "next_points": 1500, "user_official_points": 1000,
    })

    assert api.get_next_rank_points() == "+500"
    kovaaks_web.save_scores_cache.assert_not_called()


@pytest.mark.parametrize("cached", [
    None, [], {"username": "player", "points": "bad", "user_official_points": 1000},
    {"username": "player", "points": float("nan"), "user_official_points": 1000},
])
def test_malformed_cached_rank_is_repaired(api, monkeypatch, cached):
    api._scores_cache["next_rank"] = cached
    lookup = Mock(return_value={"next_points": 1500, "user_official_points": 1000})
    monkeypatch.setattr(kovaaks.api, "get_next_leaderboard_position_points", lookup)

    assert api.get_next_rank_points() == "+500"
    assert api._scores_cache["next_rank"]["points"] == 1500
    lookup.assert_called_once()


@pytest.mark.parametrize("points", ["bad", float("inf"), float("nan")])
def test_malformed_api_rank_does_not_poison_cache(api, monkeypatch, points):
    monkeypatch.setattr(kovaaks.api, "get_next_leaderboard_position_points", lambda *_: {
        "next_points": points, "user_official_points": 1000,
    })

    assert api.get_next_rank_points() == "Error"
    assert "next_rank" not in api._scores_cache
    assert api._next_rank_requests == {}
    kovaaks_web.save_scores_cache.assert_not_called()


def test_fast_background_lookup_returns_fresh_value_and_notifies_guarded_hook(api, monkeypatch):
    api._scores_cache["next_rank"] = {
        "username": "player", "points": 1200, "user_official_points": 1000,
        "timestamp": time.time() - 7200,
    }
    api.window = Mock()

    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(kovaaks_web.threading, "Thread", InlineThread)
    monkeypatch.setattr(kovaaks.api, "get_next_leaderboard_position_points", lambda *_: {
        "next_points": 1500, "user_official_points": 1000,
    })

    assert api.get_next_rank_points() == "+500"
    api.window.evaluate_js.assert_called_once_with(
        'if(window.onRankStatsUpdated) { void window.onRankStatsUpdated("player"); }'
    )
