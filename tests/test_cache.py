"""
Tests for cache loading utilities.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovaaks_gui import load_scenarios_from_cache


class TestLoadScenariosFromCache:
    def test_loads_from_valid_cache(self, sample_cache):
        result = load_scenarios_from_cache(sample_cache)
        assert len(result) == 5
        assert result[0]["scenarioName"] == "1w6ts Reload"

    def test_empty_cache_returns_empty(self):
        result = load_scenarios_from_cache({})
        assert result == []

    def test_missing_scenarios_key(self):
        result = load_scenarios_from_cache({"scores": {}})
        assert result == []

    def test_scenarios_key_empty_list(self):
        result = load_scenarios_from_cache({"scenarios": []})
        assert result == []

    def test_preserves_all_scenario_fields(self, sample_cache):
        result = load_scenarios_from_cache(sample_cache)
        first = result[0]
        assert "leaderboardId" in first
        assert "scenarioName" in first
        assert "counts" in first
        assert "entries" in first["counts"]
