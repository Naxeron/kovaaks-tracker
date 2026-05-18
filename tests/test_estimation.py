"""
Tests for estimation / interpolation functions.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_processing import (
    get_estimated_fetch_count,
    get_estimated_matching_count,
)
from constants import (
    SCENARIO_DISTRIBUTION_POINTS,
    SCENARIO_POPULARITY_DROP_OFF_POINTS,
)

# Alias used in kovaaks_gui.py — verify the pattern works
get_estimated_scenario_count = get_estimated_matching_count


# ---------------------------------------------------------------------------
# get_estimated_fetch_count
# ---------------------------------------------------------------------------

class TestGetEstimatedFetchCount:
    def test_zero_min_entries_returns_max(self):
        """min_entries=0 should return the full count."""
        result = get_estimated_fetch_count(0)
        assert result == SCENARIO_POPULARITY_DROP_OFF_POINTS[0][1]

    def test_negative_min_entries_returns_max(self):
        result = get_estimated_fetch_count(-5)
        assert result == SCENARIO_POPULARITY_DROP_OFF_POINTS[0][1]

    def test_beyond_max_returns_zero(self):
        """A threshold higher than the last point should return 0."""
        result = get_estimated_fetch_count(200000)
        assert result == 0

    def test_exact_boundary_point(self):
        """Exact match on a known data point."""
        result = get_estimated_fetch_count(1000)
        assert result == 6500  # From the data: (1000, 6500)

    def test_midpoint_interpolation(self):
        """Value between two known points should interpolate linearly."""
        # Between (0, 18000) and (10, 17500): midpoint at 5 → 17750
        result = get_estimated_fetch_count(5)
        assert result == 17750.0

    def test_high_threshold_near_max(self):
        """Value near the upper end of the curve."""
        result = get_estimated_fetch_count(100000)
        assert result == 0

    def test_returns_monotonically_decreasing(self):
        """Higher thresholds should yield fewer (or equal) estimated fetches."""
        prev = get_estimated_fetch_count(0)
        for threshold in [10, 50, 100, 500, 1000, 5000, 10000, 50000]:
            current = get_estimated_fetch_count(threshold)
            assert current <= prev, f"Not monotonic at {threshold}: {current} > {prev}"
            prev = current


# ---------------------------------------------------------------------------
# get_estimated_matching_count
# ---------------------------------------------------------------------------

class TestGetEstimatedMatchingCount:
    def test_zero_returns_max(self):
        result = get_estimated_matching_count(0)
        assert result == SCENARIO_DISTRIBUTION_POINTS[0][1]

    def test_negative_returns_max(self):
        result = get_estimated_matching_count(-10)
        assert result == SCENARIO_DISTRIBUTION_POINTS[0][1]

    def test_beyond_max_returns_zero(self):
        result = get_estimated_matching_count(200000)
        assert result == 0

    def test_exact_boundary(self):
        result = get_estimated_matching_count(1000)
        assert result == 4852  # (1000, 4852)

    def test_monotonically_decreasing(self):
        prev = get_estimated_matching_count(0)
        for t in [10, 50, 100, 500, 1000, 2000, 5000, 10000]:
            current = get_estimated_matching_count(t)
            assert current <= prev
            prev = current


# ---------------------------------------------------------------------------
# get_estimated_scenario_count (alias of matching count)
# ---------------------------------------------------------------------------

class TestGetEstimatedScenarioCount:
    def test_matches_matching_count(self):
        """This function should behave identically to get_estimated_matching_count."""
        for val in [0, 50, 100, 500, 1000, 5000, 10000]:
            assert get_estimated_scenario_count(val) == get_estimated_matching_count(val)
