"""
Tests for _get_local_stats CSV parser.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stats import get_local_stats as _get_local_stats


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
