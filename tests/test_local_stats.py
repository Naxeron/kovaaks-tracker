"""
Tests for _get_local_stats CSV parser.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovaaks.stats import get_local_stats as _get_local_stats


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
