"""
Tests for kovaaks.scoring utilities.
"""

import datetime
import math
import os
import sys
from types import MappingProxyType

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovaaks.scoring import (
    parse_iso_dt,
    parse_popularity_metrics,
    calculate_potential_score,
    calculate_global_points,
    calculate_rank_percentile,
)


class TestCalculateGlobalPoints:
    def test_all_cached_played_leaderboards_count_once(self):
        from kovaaks.catalog import freeze_catalog

        scenarios = freeze_catalog([
            {"leaderboardId": 1, "counts": {"entries": 1000}},
            {"leaderboardId": "1", "counts": {"entries": 1000}},
            {"leaderboardId": "small", "counts": {"entries": "20"}},
            {"leaderboardId": "hidden", "counts": {"entries": 500}},
            {"leaderboardId": "last", "counts": {"entries": 10}},
            {"leaderboardId": "unplayed", "counts": {"entries": 5000}},
            {"leaderboardId": "friends", "counts": {"entries": 5000}},
        ])
        scores = MappingProxyType({
            "1": {"user": {"rank": 100}},
            "small": {"user": {"rank": "2"}},
            "hidden": {"user": {"rank": 50}},
            "last": {"user": {"rank": 10}},
            "friends": {"friends": [{"rank": 1}]},
            "missing-from-catalog": {"user": {"rank": 1}},
        })

        assert calculate_global_points(scenarios, scores) == 900 + 18 + 450

    @pytest.mark.parametrize("entries,rank", [
        (100, 0), (100, -1), (100, 101), (0, 1), (-5, 1),
        (100, None), (None, 1), (100, "bad"), ("bad", 1),
        (100, float("inf")), (float("inf"), 1),
        (100, float("nan")), (float("nan"), 1),
        (100, "Infinity"), ("NaN", 1),
        (100, True), (True, 1), (100, 1.5), (100.5, 1),
        (100, []), ({}, 1), (10 ** 1000, 1), (100, 10 ** 1000),
    ])
    def test_invalid_counts_and_ranks_do_not_add_or_subtract_points(self, entries, rank):
        scenarios = [
            {"leaderboardId": "valid", "counts": {"entries": 20}},
            {"leaderboardId": "bad", "counts": {"entries": entries}},
        ]
        scores = {"valid": {"user": {"rank": 1}}, "bad": {"user": {"rank": rank}}}

        assert calculate_global_points(scenarios, scores) == 19

    @pytest.mark.parametrize("scenarios,scores", [
        (None, {}), ({}, {}), ("catalog", {}), ([], None), ([], []),
        ([None, [], {}, {"leaderboardId": None}, {"leaderboardId": []}], {}),
        ([{"leaderboardId": "1", "counts": None}], {"1": {"user": {"rank": 1}}}),
        ([{"leaderboardId": "1", "counts": {"entries": 100}}], {"1": None}),
        ([{"leaderboardId": "1", "counts": {"entries": 100}}], {"1": {"user": None}}),
        ([{"leaderboardId": " ", "counts": {"entries": 100}}], {" ": {"user": {"rank": 1}}}),
    ])
    def test_malformed_or_missing_data_is_ignored(self, scenarios, scores):
        assert calculate_global_points(scenarios, scores) == 0

    def test_read_only_mappings_and_integral_numbers_are_supported(self):
        scenarios = (MappingProxyType({
            "leaderboardId": "1", "counts": MappingProxyType({"entries": 100.0}),
        }),)
        scores = MappingProxyType({
            "1": MappingProxyType({"user": MappingProxyType({"rank": "2.0"})}),
        })

        assert calculate_global_points(scenarios, scores) == 98


class TestParseIsoDt:
    def test_parses_z_suffix(self):
        dt = parse_iso_dt("2026-07-08T20:53:22Z")
        assert dt == datetime.datetime(2026, 7, 8, 20, 53, 22)
        assert dt.tzinfo is None

    def test_parses_short_date(self):
        dt = parse_iso_dt("2026-07-08")
        assert dt == datetime.datetime(2026, 7, 8, 0, 0, 0)
        assert dt.tzinfo is None

    def test_parses_standard_iso(self):
        dt = parse_iso_dt("2026-07-08T15:30:45")
        assert dt == datetime.datetime(2026, 7, 8, 15, 30, 45)


class TestParsePopularityMetrics:
    def test_empty_history(self):
        trend, new_entries = parse_popularity_metrics({})
        assert trend == 0.0
        assert new_entries == 0

    def test_single_history_point(self):
        trend, new_entries = parse_popularity_metrics({"2026-07-08T12:00:00Z": 100})
        assert trend == 0.0
        assert new_entries == 0

    def test_multiple_points_within_limits(self):
        # 1 day difference, entries increased by 100
        hist = {
            "2026-07-07T12:00:00Z": 1000,
            "2026-07-08T12:00:00Z": 1100,
        }
        trend, new_entries = parse_popularity_metrics(hist)
        # diff in seconds = 86400 (1 day), trend = (1100 - 1000) / 1.0 = 100.0
        assert math.isclose(trend, 100.0)
        assert new_entries == 100


class TestCalculatePotentialScore:
    @pytest.mark.parametrize("rank,entries", [
        (0, 1000), (500, 0), (1500, 1000), (-1, 1000),
        ("bad", 1000), (500, None), (500, "bad"),
        (float("nan"), 1000), (500, float("inf")),
        (True, 1000), (500, True), (1.5, 1000), (500, 1000.5),
        (10 ** 1000, 1000), (500, 10 ** 1000),
    ])
    def test_invalid_parameters_return_zero(self, rank, entries):
        assert calculate_potential_score(rank, entries) == 0
        assert calculate_rank_percentile(rank, entries) is None

    def test_valid_percentile_accepts_integral_strings(self):
        assert calculate_rank_percentile("200.0", "1000") == 80
        assert calculate_rank_percentile(None, 1000) is None

    def test_base_unplayed_score(self):
        # Unplayed scenarios start at zero contribution, so all target points count.
        assert calculate_potential_score(None, 1000, expected_pct=80) == 800
        assert calculate_potential_score(None, 1000) == 500
        assert calculate_potential_score(None, 1000, expected_pct=0) == 0

    def test_more_attainable_points_outrank_smaller_board(self):
        small = calculate_potential_score(500, 1000, expected_pct=80)
        large = calculate_potential_score(30000, 100000, expected_pct=80)
        assert (small, large) == (300, 10000)
        assert large > small

    def test_equal_percentile_gaps_scale_with_points(self):
        assert calculate_potential_score(5000, 10000, expected_pct=80) == 10 * (
            calculate_potential_score(500, 1000, expected_pct=80))

    def test_time_decay_is_neutral(self):
        now = datetime.datetime.now()
        # Played today vs played 20 days ago: time away is not evidence of gain.
        recent = {"last_played": now.isoformat(), "runs_today": 0}
        old = {"last_played": (now - datetime.timedelta(days=20)).isoformat()}
        assert calculate_potential_score(100, 1000, recent, now, 1) == (
            calculate_potential_score(100, 1000, old, now, 1))

    def test_daily_fatigue_and_popularity_are_neutral(self):
        # The old fatigue factor exp(-24/12) unfairly suppressed rested players.
        fresh = calculate_potential_score(500, 1000, {}, competition_multiplier=0.2)
        tired = calculate_potential_score(500, 1000, {"runs_today": 24},
                                          competition_multiplier=10)
        assert fresh == tired

    def test_plateau_penalty_requires_real_observations(self):
        base = calculate_potential_score(500, 1000, expected_pct=80)
        for stats in ({"runs_since_recent_pb": 999},
                      {"pb_observations": 2, "runs_since_pb": 999},
                      {"pb_observations": 21, "runs_since_pb": 20}):
            assert calculate_potential_score(500, 1000, stats, expected_pct=80) == base
        priorities = [calculate_potential_score(500, 1000, {
            "pb_observations": n + 1, "runs_since_pb": n,
        }, expected_pct=80) for n in (20, 21, 40, 1000)]
        assert priorities == sorted(priorities, reverse=True)
        assert priorities[-1] >= 0.75 * base
        assert priorities[0] - priorities[1] < 0.02 * base

    def test_sparse_trends_stay_neutral_and_strong_trends_are_bounded(self):
        base = calculate_potential_score(500, 1000, expected_pct=80)
        for count in (0, 1, 2, 4):
            stats = {"trend": 2, "count": 100, "recent_sample_count": count}
            assert calculate_potential_score(500, 1000, stats, expected_pct=80) == base
        for trend in (0, 0.99, 1.02, 1.0201, 2):
            stats = {"trend": trend, "recent_sample_count": 10}
            assert 0.9 * base <= calculate_potential_score(
                500, 1000, stats, expected_pct=80) <= 1.1 * base

    def test_unplayed_first_submission_ignores_local_plateau(self):
        stats = {"pb_observations": 100, "runs_since_pb": 99,
                 "recent_sample_count": 10, "trend": 0.5}
        assert calculate_potential_score(None, 1000, stats, expected_pct=80) == 800

    def test_expected_pct_below_average(self):
        # Rank 500/1000 is 50th percentile; an 80th-percentile target means +300.
        below = calculate_potential_score(500, 1000, expected_pct=80)
        # At the category baseline, use 10% of remaining rank headroom instead.
        equal = calculate_potential_score(500, 1000, expected_pct=50)
        assert below == 300
        assert equal == 50
        assert below > equal

    def test_expected_pct_above_average(self):
        # Above-category scores retain a stretch target; rank 1 has no upside.
        assert calculate_potential_score(200, 1000, expected_pct=60) == 20
        assert calculate_potential_score(1, 1000, expected_pct=100) == 0
        assert calculate_potential_score(None, 1, expected_pct=100) == 0
        # Preserve a nonzero opportunity at the smallest improvable rank.
        assert calculate_potential_score(2, 1000, expected_pct=50) == 1

    def test_priority_never_exceeds_remaining_points(self):
        stats = {"trend": 2, "recent_sample_count": 10}
        assert calculate_potential_score(500, 1000, stats, expected_pct=100) == 499
        assert calculate_potential_score(None, 1000, stats, expected_pct=100) == 999

    @pytest.mark.parametrize("stats", [None, [], {"trend": "bad"}, {
        "trend": float("nan"), "recent_sample_count": "bad",
        "pb_observations": float("inf"), "runs_since_pb": "bad",
    }, {
        "trend": 10 ** 1000, "recent_sample_count": 10 ** 1000,
    }])
    def test_missing_or_malformed_evidence_is_neutral(self, stats):
        assert calculate_potential_score("500", "1000", stats, expected_pct=80) == 300

    @pytest.mark.parametrize("target", [None, "bad", float("nan"), float("inf")])
    def test_invalid_target_falls_back_to_neutral_percentile(self, target):
        assert calculate_potential_score(None, 1000, expected_pct=target) == 500
