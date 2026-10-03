"""
Tests for _get_local_stats CSV parser.
"""
import datetime
import os
import sys
from copy import deepcopy

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovaaks.stats import get_local_stats as _get_local_stats
import kovaaks.stats as stats_helpers


def _write_score(directory, played_at, score):
    filename = f"Scenario A - Challenge - {played_at:%Y.%m.%d-%H.%M.%S} Stats.csv"
    path = directory / filename
    path.write_text(f"Score:,{score}\n", encoding="utf-8")
    return path


class TestGetLocalStats:
    def test_returns_dict(self, stats_dir):
        result = _get_local_stats(stats_dir)
        assert isinstance(result, dict)

    def test_finds_known_scenario(self, stats_dir):
        result = _get_local_stats(stats_dir)
        assert "1w6ts Reload" in result

    def test_counts_runs(self, stats_dir):
        """Should count all stats files for a scenario."""
        result = _get_local_stats(stats_dir)
        assert result["1w6ts Reload"]["count"] == 5

    def test_single_run_scenario(self, stats_dir):
        result = _get_local_stats(stats_dir)
        assert "Pasu Voltaic Easy" in result
        assert result["Pasu Voltaic Easy"]["count"] == 1

    def test_last_played_is_most_recent(self, stats_dir):
        result = _get_local_stats(stats_dir)
        lp = result["1w6ts Reload"]["last_played"]
        assert isinstance(lp, datetime.datetime)
        # Most recent should be within the last hour
        assert (datetime.datetime.now() - lp).total_seconds() < 3600

    def test_trend_computed_for_multi_run(self, stats_dir):
        """Scenarios with >= 2 runs should have a non-default trend."""
        result = _get_local_stats(stats_dir)
        # 5 runs → trend should be computed
        trend = result["1w6ts Reload"]["trend"]
        assert isinstance(trend, float)
        # Trend is clamped between 0.5 and 2.0
        assert 0.5 <= trend <= 2.0

    def test_trend_default_for_single_run(self, stats_dir):
        result = _get_local_stats(stats_dir)
        assert result["Pasu Voltaic Easy"]["trend"] == 1.0

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        result = _get_local_stats(str(tmp_path / "nope"))
        assert result == {}

    def test_empty_dir_returns_empty(self, tmp_path):
        empty = tmp_path / "empty_stats"
        empty.mkdir()
        result = _get_local_stats(str(empty))
        assert result == {}

    def test_malformed_csv_skipped(self, tmp_path):
        """Files without the expected date pattern should be skipped gracefully."""
        stats = tmp_path / "stats"
        stats.mkdir()
        # Missing the third part of the rsplit(" - ", 2)
        (stats / "BadName Stats.csv").write_text("Score:,100\n", encoding="utf-8")
        result = _get_local_stats(str(stats))
        assert result == {}

    def test_recent_scores_not_in_output(self, stats_dir):
        """The intermediate 'recent_scores' key should be cleaned up."""
        result = _get_local_stats(stats_dir)
        for data in result.values():
            assert "recent_scores" not in data

    def test_incremental_stats_cache(self, tmp_path):
        """Test that get_local_stats uses the cached stats and updates correctly."""
        stats_dir = tmp_path / "stats"
        stats_dir.mkdir()
        
        # Write first stat file
        f1 = stats_dir / "1w6ts Reload - Challenge - 2026.05.10-12.00.00 Stats.csv"
        f1.write_text("Score:,100.5\n", encoding="utf-8")
        
        cache = {
            "known_stat_files": [],
            "local_stats": {}
        }
        
        # First call: populates cache
        res1 = _get_local_stats(str(stats_dir), cache)
        assert "1w6ts Reload" in res1
        assert res1["1w6ts Reload"]["count"] == 1
        assert "1w6ts Reload" in cache["local_stats"]
        assert cache["local_stats"]["1w6ts Reload"]["count"] == 1
        assert f1.name in cache["known_stat_files"]
        
        # Second call with no changes: should not read file again (we modify the file on disk to verify)
        f1.write_text("Score:,999.0\n", encoding="utf-8")
        res2 = _get_local_stats(str(stats_dir), cache)
        assert cache["local_stats"]["1w6ts Reload"]["recent_scores"][0][1] == 100.5
        
        # Write second stat file
        f2 = stats_dir / "1w6ts Reload - Challenge - 2026.05.10-13.00.00 Stats.csv"
        f2.write_text("Score:,120.0\n", encoding="utf-8")
        
        # Third call: parses only new file
        res3 = _get_local_stats(str(stats_dir), cache)
        assert res3["1w6ts Reload"]["count"] == 2
        assert f2.name in cache["known_stat_files"]
        
        # Check that recent scores contains both the old cached score and the new score
        recent = cache["local_stats"]["1w6ts Reload"]["recent_scores"]
        assert len(recent) == 2
        assert recent[0][1] == 100.5
        assert recent[1][1] == 120.0

    def test_dynamic_runs_today(self, tmp_path):
        """Test that runs_today is dynamically calculated correctly relative to now."""
        stats_dir = tmp_path / "stats"
        stats_dir.mkdir()
        
        # Write a file played today
        now = datetime.datetime.now()
        now_str = now.strftime("%Y.%m.%d-%H.%M.%S")
        f1 = stats_dir / f"1w6ts Reload - Challenge - {now_str} Stats.csv"
        f1.write_text("Score:,100.0\n", encoding="utf-8")
        
        # Write a file played 2 days ago
        two_days_ago = now - datetime.timedelta(days=2)
        old_str = two_days_ago.strftime("%Y.%m.%d-%H.%M.%S")
        f2 = stats_dir / f"1w6ts Reload - Challenge - {old_str} Stats.csv"
        f2.write_text("Score:,95.0\n", encoding="utf-8")
        
        res = _get_local_stats(str(stats_dir))
        assert res["1w6ts Reload"]["count"] == 2
        assert res["1w6ts Reload"]["runs_today"] == 1

    def test_newly_played_scenarios_tracked_in_cache(self, tmp_path):
        """Test that get_local_stats records newly played scenarios in newly_played_scenarios."""
        stats_dir = tmp_path / "stats"
        stats_dir.mkdir()
        
        f1 = stats_dir / "1w6ts Reload - Challenge - 2026.05.10-12.00.00 Stats.csv"
        f1.write_text("Score:,100.5\n", encoding="utf-8")
        
        cache = {
            "known_stat_files": [],
            "local_stats": {}
        }
        
        _get_local_stats(str(stats_dir), cache)
        assert "newly_played_scenarios" in cache
        assert "1w6ts Reload" in cache["newly_played_scenarios"]

    def test_legacy_cache_rebuilt_once_then_historical_files_are_not_read(self, tmp_path, monkeypatch):
        """Repair old watcher markers without reparsing history every refresh."""
        first = tmp_path / "Scenario A - Challenge - 2026.09.29-12.00.00 Stats.csv"
        second = tmp_path / "Scenario A - Challenge - 2026.09.30-12.00.00 Stats.csv"
        first.write_text("Score:,100\n", encoding="utf-8")
        second.write_text("Score:,200\n", encoding="utf-8")
        cache = {
            "known_stat_files": [first.name, second.name],
            "local_stats": {"Scenario A": {
                "count": 1,
                "last_played": "2026-09-29T12:00:00",
                "recent_scores": [["2026-09-29T12:00:00", 100]],
            }},
        }

        result = _get_local_stats(str(tmp_path), cache)

        assert result["Scenario A"]["count"] == 2
        assert cache["local_stats_version"] == stats_helpers.LOCAL_STATS_CACHE_VERSION
        assert [score for _, score in cache["local_stats"]["Scenario A"]["recent_scores"]] == [100, 200]
        assert cache.pop("_dirty") is True
        previous = deepcopy(cache)

        def unexpected_read(*args, **kwargs):
            raise AssertionError("Historical CSV files must not be reopened")

        monkeypatch.setattr(stats_helpers, "open", unexpected_read, raising=False)
        assert _get_local_stats(str(tmp_path), cache)["Scenario A"]["count"] == 2
        assert cache == previous

    @pytest.mark.parametrize("contents", [
        "", "Scenario:,Scenario A\n", "Score:,invalid\n", "Score:,nan\n", "Score:,inf\n",
    ])
    def test_incomplete_file_is_retried_after_valid_score_arrives(self, tmp_path, contents):
        """Pending files must not commit count/recency/markers or dirty the cache."""
        path = tmp_path / "Scenario A - Challenge - 2026.09.30-12.00.00 Stats.csv"
        path.write_text(contents, encoding="utf-8")
        cache = {
            "local_stats_version": stats_helpers.LOCAL_STATS_CACHE_VERSION,
            "known_stat_files": [],
            "local_stats": {},
        }
        previous = deepcopy(cache)

        assert _get_local_stats(str(tmp_path), cache) == {}
        assert cache == previous

        path.write_text("Score:,120\n", encoding="utf-8")
        assert _get_local_stats(str(tmp_path), cache)["Scenario A"]["count"] == 1
        assert cache["local_stats"]["Scenario A"]["recent_scores"] == [["2026-09-30T12:00:00", 120]]
        assert cache["known_stat_files"] == [path.name]
        assert cache["newly_played_scenarios"] == ["Scenario A"]
        assert cache.pop("_dirty") is True

        _get_local_stats(str(tmp_path), cache)
        assert cache["local_stats"]["Scenario A"]["count"] == 1
        assert "_dirty" not in cache

    def test_unreadable_file_is_retried_without_committing_statistics(self, tmp_path, monkeypatch):
        path = tmp_path / "Scenario A - Challenge - 2026.09.30-12.00.00 Stats.csv"
        path.write_text("Score:,120\n", encoding="utf-8")
        cache = {
            "local_stats_version": stats_helpers.LOCAL_STATS_CACHE_VERSION,
            "known_stat_files": [],
            "local_stats": {},
        }
        previous = deepcopy(cache)

        def unreadable(*args, **kwargs):
            raise PermissionError("CSV temporarily locked")

        with monkeypatch.context() as patch:
            patch.setattr(stats_helpers, "open", unreadable, raising=False)
            assert _get_local_stats(str(tmp_path), cache) == {}
            assert cache == previous

        assert _get_local_stats(str(tmp_path), cache)["Scenario A"]["count"] == 1
        assert cache["known_stat_files"] == [path.name]

    def test_zero_score_is_valid(self, tmp_path):
        path = tmp_path / "Scenario A - Challenge - 2026.09.30-12.00.00 Stats.csv"
        path.write_text("Score:,0\n", encoding="utf-8")
        cache = {}

        assert _get_local_stats(str(tmp_path), cache)["Scenario A"]["count"] == 1
        assert cache["local_stats"]["Scenario A"]["recent_scores"] == [["2026-09-30T12:00:00", 0]]

    @pytest.mark.parametrize("use_cache", [False, True])
    def test_two_declining_runs_do_not_invent_a_plateau(self, tmp_path, use_cache):
        first_run = datetime.datetime(2026, 9, 20, 12)
        _write_score(tmp_path, first_run, 100)
        _write_score(tmp_path, first_run + datetime.timedelta(minutes=1), 99)

        result = _get_local_stats(str(tmp_path), {} if use_cache else None)["Scenario A"]

        assert result["runs_since_recent_pb"] == 1
        assert result["runs_since_pb"] == 1
        assert result["pb_observations"] == 2
        assert result["recent_sample_count"] == 2
        assert result["best_score"] == 100

    @pytest.mark.parametrize("use_cache", [False, True])
    def test_observed_pb_survives_the_recent_trend_window(self, tmp_path, use_cache):
        first_run = datetime.datetime(2026, 9, 20, 12)
        for offset in range(25):
            _write_score(tmp_path, first_run + datetime.timedelta(minutes=offset), 100 - offset)
        cache = {} if use_cache else None

        result = _get_local_stats(str(tmp_path), cache)["Scenario A"]

        assert result["count"] == 25
        assert result["best_score"] == 100
        assert result["runs_since_pb"] == 24
        assert result["pb_observations"] == 25
        assert result["recent_sample_count"] == 10
        assert result["runs_since_recent_pb"] == 9
        assert "recent_scores" not in result
        if cache is not None:
            assert len(cache["local_stats"]["Scenario A"]["recent_scores"]) == 10
            for field in ("best_score", "runs_since_pb", "pb_observations", "recent_sample_count"):
                assert cache["local_stats"]["Scenario A"][field] == result[field]

    @pytest.mark.parametrize("use_cache", [False, True])
    def test_only_a_strict_score_improvement_resets_pb_age(self, tmp_path, use_cache):
        first_run = datetime.datetime(2026, 9, 20, 12)
        cache = {} if use_cache else None
        expected_ages = [0, 1, 2, 0, 1, 2]
        for offset, score in enumerate([100, 99, 100, 101, 101, 100]):
            _write_score(tmp_path, first_run + datetime.timedelta(minutes=offset), score)
            result = _get_local_stats(str(tmp_path), cache)["Scenario A"]

            assert result["runs_since_pb"] == expected_ages[offset]
            assert result["runs_since_recent_pb"] == expected_ages[offset]
            assert result["pb_observations"] == offset + 1

        assert result["best_score"] == 101

    def test_pb_updates_only_read_new_files_and_unchanged_refresh_stays_clean(self, tmp_path, monkeypatch):
        first_run = datetime.datetime(2026, 9, 20, 12)
        for offset in range(15):
            _write_score(tmp_path, first_run + datetime.timedelta(minutes=offset), 100 - offset)
        cache = {}
        _get_local_stats(str(tmp_path), cache)
        cache.pop("_dirty")
        new_file = _write_score(tmp_path, first_run + datetime.timedelta(minutes=15), 85)
        real_open = open
        opened = []

        def only_new_file(path, *args, **kwargs):
            assert str(path) == str(new_file), "Historical CSV files must not be reopened"
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(stats_helpers, "open", only_new_file, raising=False)
        result = _get_local_stats(str(tmp_path), cache)
        assert opened == [str(new_file)]
        assert result["Scenario A"]["runs_since_pb"] == 15
        assert result["Scenario A"]["pb_observations"] == 16
        assert cache.pop("_dirty") is True
        previous = deepcopy(cache)

        assert _get_local_stats(str(tmp_path), cache) == result
        assert cache == previous
        assert opened == [str(new_file)]

    @pytest.mark.parametrize("minutes, score, expected_best, expected_age", [
        (-1, 99, 100, 2),
        (-1, 100, 100, 3),
        (-1, 110, 110, 3),
        (1, 99, 100, 3),
    ])
    def test_older_arriving_files_update_pb_age_without_rereading_history(
        self, tmp_path, monkeypatch, minutes, score, expected_best, expected_age
    ):
        first_run = datetime.datetime(2026, 9, 20, 12)
        for offset, initial_score in [(0, 100), (2, 90), (4, 100)]:
            _write_score(tmp_path, first_run + datetime.timedelta(minutes=offset), initial_score)
        cache = {}
        _get_local_stats(str(tmp_path), cache)
        new_file = _write_score(tmp_path, first_run + datetime.timedelta(minutes=minutes), score)
        real_open = open

        def only_new_file(path, *args, **kwargs):
            assert str(path) == str(new_file), "Historical CSV files must not be reopened"
            return real_open(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(stats_helpers, "open", only_new_file, raising=False)
            result = _get_local_stats(str(tmp_path), cache)["Scenario A"]

        assert result["last_played"] == first_run + datetime.timedelta(minutes=4)
        assert result["best_score"] == expected_best
        assert result["runs_since_pb"] == expected_age
        assert result["pb_observations"] == 4
        assert result == _get_local_stats(str(tmp_path))["Scenario A"]

    def test_version_two_cache_rebuilds_missing_pb_history_once(self, tmp_path, monkeypatch):
        first_run = datetime.datetime(2026, 9, 20, 12)
        paths = [
            _write_score(tmp_path, first_run + datetime.timedelta(minutes=offset), 100 - offset)
            for offset in range(15)
        ]
        cache = {
            "local_stats_version": 2,
            "known_stat_files": [path.name for path in paths],
            "local_stats": {"Scenario A": {
                "count": 15,
                "last_played": (first_run + datetime.timedelta(minutes=14)).isoformat(),
                "runs_since_recent_pb": 999,
                "recent_scores": [],
            }},
        }

        result = _get_local_stats(str(tmp_path), cache)

        assert result["Scenario A"]["runs_since_pb"] == 14
        assert result["Scenario A"]["best_score"] == 100
        assert result["Scenario A"]["pb_observations"] == 15
        assert result["Scenario A"]["runs_since_recent_pb"] == 9
        assert cache["local_stats_version"] == stats_helpers.LOCAL_STATS_CACHE_VERSION
        assert cache.pop("_dirty") is True
        previous = deepcopy(cache)

        def unexpected_read(*args, **kwargs):
            raise AssertionError("Historical CSV files must not be reopened")

        monkeypatch.setattr(stats_helpers, "open", unexpected_read, raising=False)
        assert _get_local_stats(str(tmp_path), cache) == result
        assert cache == previous
