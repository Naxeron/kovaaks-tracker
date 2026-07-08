"""
Tests for kovaaks.scoring utilities.
"""

import datetime
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovaaks.scoring import (
    parse_iso_dt,
    parse_popularity_metrics,
    calculate_potential_score,
)


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
    def test_invalid_parameters_return_zero(self):
        assert calculate_potential_score(0, 1000, {}, datetime.datetime.now(), 1.0) == 0
        assert calculate_potential_score(500, 0, {}, datetime.datetime.now(), 1.0) == 0
        assert calculate_potential_score(1500, 1000, {}, datetime.datetime.now(), 1.0) == 0

    def test_base_unplayed_score(self):
        now = datetime.datetime.now()
        lstats = {}
        # Unplayed scenario potential
        potential = calculate_potential_score(500, 1000, lstats, now, 1.0)
        assert potential > 0
        # Time factor should be 1.5, fatigue_factor 1.0, plateau_penalty 1.0, trend_factor 1.0
        # skill_gap = 1.0 - 50.0/100.0 = 0.5
        # log_weight = log10(500) ~= 2.69897
        # base_potential = 2.69897 * 0.5 = 1.349485
        # final = (1.349485 * 1000) * 1.5 * 1.0 * 0.999715 * 1.0 * 1.0 = 2023.65
        assert potential == 2023

    def test_time_decay(self):
        now = datetime.datetime.now()
        # Played today vs played 20 days ago
        lstats_recent = {"last_played": now.isoformat(), "runs_today": 0}
        lstats_old = {"last_played": (now - datetime.timedelta(days=20)).isoformat(), "runs_today": 0}
        
        pot_recent = calculate_potential_score(100, 1000, lstats_recent, now, 1.0)
        pot_old = calculate_potential_score(100, 1000, lstats_old, now, 1.0)
        
        # Played recently should have lower priority (less potential) than played long ago
        assert pot_recent < pot_old

    def test_fatigue_penalty(self):
        now = datetime.datetime.now()
        lstats_fresh = {"runs_today": 0}
        lstats_tired = {"runs_today": 12}  # fatigue factor e^(-12/12) = e^-1 ~= 0.368
        
        pot_fresh = calculate_potential_score(500, 1000, lstats_fresh, now, 1.0)
        pot_tired = calculate_potential_score(500, 1000, lstats_tired, now, 1.0)
        
        assert pot_tired < pot_fresh
        assert math.isclose(pot_tired / pot_fresh, math.exp(-1), rel_tol=0.01)

    def test_plateau_penalty(self):
        now = datetime.datetime.now()
        # No trend improvement and many runs since recent PB should decrease potential
        lstats_improving = {"trend": 1.05, "runs_since_recent_pb": 40}
        lstats_plateau = {"trend": 1.0, "runs_since_recent_pb": 40}
        
        pot_improving = calculate_potential_score(500, 1000, lstats_improving, now, 1.0)
        pot_plateau = calculate_potential_score(500, 1000, lstats_plateau, now, 1.0)
        
        assert pot_plateau < pot_improving
