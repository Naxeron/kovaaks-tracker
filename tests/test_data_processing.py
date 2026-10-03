"""
Tests for _rebuild_data business logic.

Since _rebuild_data is a method on the GUI class (KovaaksApp) that calls
tkinter methods, we extract and test the core computation logic by mocking
the GUI layer and directly invoking the method.
"""
import datetime
import math
import os
import sys
import threading
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovaaks import app as kovaaks_web


def _make_app_stub(scenario_info, user_by_lid, friends_by_lid,
                   scores_cache=None, cfg=None, stats_dir=None):
    """Create a minimal mock of KovaaksApp with just the data attributes
    needed by _rebuild_data, without instantiating a real Tk window."""
    app = MagicMock()
    app._scenario_info = scenario_info
    app._user_by_lid = user_by_lid
    app._friends_by_lid = friends_by_lid
    app._scores_cache = scores_cache or {"entry_history": {}}
    app._scores_cache.setdefault("scenarios", [
        {"leaderboardId": lid, "scenarioName": info["name"],
         "counts": {"entries": info["entries"]}}
        for lid, info in scenario_info.items()
    ])
    app._scores_cache.setdefault("scores", {
        lid: {"user": user} for lid, user in user_by_lid.items()
    })
    app._cfg = cfg or {"stats_dir": "/nonexistent", "min_entries": "1000",
                       "always_show_total_points": True}
    app._global_points_sum = 0
    app._global_potential_points_sum = 0
    app._global_projected_gain_sum = 0
    app._all_data = []
    app._sort_state = None
    app._filters = {}
    app._hidden_scenarios = set()

    # Local stats cache support
    app._local_stats_cache = None
    app._local_stats_dirty = True

    # Mock the methods that _rebuild_data calls
    app._get_stats_dir.return_value = stats_dir or "/nonexistent"
    app.after = MagicMock()

    # Bind the real _rebuild_data to our stub
    app._data_lock = threading.RLock()
    app._refresh_local_stats = kovaaks_web.KovaaksAPI._refresh_local_stats.__get__(app)
    app._build_data_rows = kovaaks_web.KovaaksAPI._build_data_rows.__get__(app)
    orig_rebuild = kovaaks_web.KovaaksAPI._rebuild_data.__get__(app)
    def rebuild_data_wrapper(*args, **kwargs):
        played, unplayed = orig_rebuild(*args, **kwargs)
        app._all_data = played + unplayed
        return len(played), len(unplayed)

    app._rebuild_data = rebuild_data_wrapper
    app._apply_current_sort = MagicMock()
    app._apply_filter = MagicMock()

    return app


class TestRebuildDataRows:
    def test_generates_rows_for_all_scenarios(self, sample_scenarios, sample_user_scores, sample_friend_scores):
        info = {}
        for s in sample_scenarios:
            lid = str(s["leaderboardId"])
            info[lid] = {"name": s["scenarioName"],
                         "entries": s["counts"]["entries"]}

        # Convert sample scores to use string keys matching the code
        user = {str(k): v for k, v in sample_user_scores.items()}
        friends = {str(k): v for k, v in sample_friend_scores.items()}

        app = _make_app_stub(info, user, friends)

        with patch("kovaaks.app._get_local_stats", return_value={}):
            played, unplayed = app._rebuild_data()

        # lid-4 is below threshold but still in scenario_info for this test
        assert len(app._all_data) == len(sample_scenarios)
        assert played + unplayed == len(sample_scenarios)

    def test_played_count(self, sample_scenarios, sample_user_scores, sample_friend_scores):
        info = {}
        for s in sample_scenarios:
            lid = str(s["leaderboardId"])
            info[lid] = {"name": s["scenarioName"],
                         "entries": s["counts"]["entries"]}

        user = {str(k): v for k, v in sample_user_scores.items()}
        friends = {str(k): v for k, v in sample_friend_scores.items()}

        app = _make_app_stub(info, user, friends)
        with patch("kovaaks.app._get_local_stats", return_value={}):
            played, unplayed = app._rebuild_data()

        # lid-1: user + friends → played
        # lid-2: user + friends → played
        # lid-3: friends only → played
        # lid-4: neither → unplayed
        # lid-5: user only → played
        assert played == 4
        assert unplayed == 1

    def test_percentile_calculation(self):
        info = {"lid-x": {"name": "Test Scenario", "entries": 10000}}
        user = {"lid-x": {"rank": 100, "score": 5000.0, "date": "2026-01-01"}}

        app = _make_app_stub(info, user, {})
        with patch("kovaaks.app._get_local_stats", return_value={}):
            app._rebuild_data()

        row = app._all_data[0]
        # Percentile = (1 - 100/10000) * 100 = 99.00%
        assert row["Percentile"] == "99.00%"

    def test_rank_diff_computation(self):
        info = {"lid-x": {"name": "Test", "entries": 5000}}
        user = {"lid-x": {"rank": 200, "score": 3000.0, "date": "2026-01-01"}}
        friends = {"lid-x": [{"friend": "Rival", "rank": 100, "score": 3500.0, "date": "2026-01-01"}]}

        app = _make_app_stub(info, user, friends)
        with patch("kovaaks.app._get_local_stats", return_value={}):
            app._rebuild_data()

        row = app._all_data[0]
        # User rank 200 - friend rank 100 = 100
        assert row["Rank Diff"] == "100"

    def test_friend_percentile(self):
        info = {"lid-x": {"name": "Test", "entries": 10000}}
        friends = {"lid-x": [{"friend": "A", "rank": 50, "score": 9000.0, "date": "2026-01-01"}]}

        app = _make_app_stub(info, {}, friends)
        with patch("kovaaks.app._get_local_stats", return_value={}):
            app._rebuild_data()

        row = app._all_data[0]
        # (1 - 50/10000) * 100 = 99.50%
        assert row["Friend Percentile"] == "99.50%"

    def test_global_points_accumulated(self):
        info = {
            "lid-a": {"name": "S1", "entries": 5000},
            "lid-b": {"name": "S2", "entries": 3000},
        }
        user = {
            "lid-a": {"rank": 100, "score": 1000, "date": "2026-01-01"},
            "lid-b": {"rank": 50, "score": 2000, "date": "2026-01-01"},
        }

        app = _make_app_stub(info, user, {})
        with patch("kovaaks.app._get_local_stats", return_value={}):
            app._rebuild_data()

        # Points = (entries - rank) summed
        # (5000-100) + (3000-50) = 4900 + 2950 = 7850
        assert app._global_points_sum == 7850

    def test_global_potential_points(self):
        info = {"lid-a": {"name": "S1", "entries": 5000}}
        user = {"lid-a": {"rank": 100, "score": 1000, "date": "2026-01-01"}}

        app = _make_app_stub(info, user, {})
        with patch("kovaaks.app._get_local_stats", return_value={}):
            app._rebuild_data()

        # Potential = (rank - 1)
        assert app._global_potential_points_sum == 99

    def test_friends_only_scenario_potential_points(self):
        """When friends have played but user hasn't, potential = entries - 1."""
        info = {"lid-f": {"name": "FriendsOnly", "entries": 8000}}
        friends = {"lid-f": [{"friend": "Buddy", "rank": 100, "score": 5000.0, "date": ""}]}

        app = _make_app_stub(info, {}, friends)
        with patch("kovaaks.app._get_local_stats", return_value={}):
            app._rebuild_data()

        # Friends-only potential = (entries - 1)
        assert app._global_potential_points_sum == 7999
        assert app._all_data[0]["Potential"] == "4000"

    def test_fully_unplayed_no_potential_accumulated(self):
        """Completely unplayed scenarios (no user, no friends) now contribute potential."""
        info = {"lid-u": {"name": "Unplayed", "entries": 8000}}

        app = _make_app_stub(info, {}, {})
        with patch("kovaaks.app._get_local_stats", return_value={}):
            app._rebuild_data()

        assert app._global_potential_points_sum == 7999
        assert app._all_data[0]["Potential"] == "4000"

    def test_best_friend_is_highest_ranked(self):
        info = {"lid-x": {"name": "Test", "entries": 5000}}
        friends = {"lid-x": [
            {"friend": "Slow", "rank": 500, "score": 100.0, "date": ""},
            {"friend": "Fast", "rank": 10, "score": 9999.0, "date": ""},
            {"friend": "Mid", "rank": 200, "score": 500.0, "date": ""},
        ]}

        app = _make_app_stub(info, {}, friends)
        with patch("kovaaks.app._get_local_stats", return_value={}):
            app._rebuild_data()

        row = app._all_data[0]
        assert row["Top Friend"] == "Fast"
        assert row["Friend Rank"] == "10"

    def test_potential_score_computed_for_played(self):
        """Potential should be computed for scenarios where user has rank + entries."""
        info = {"lid-p": {"name": "PotentialTest", "entries": 10000}}
        user = {"lid-p": {"rank": 500, "score": 3000.0, "date": "2026-01-01"}}

        app = _make_app_stub(info, user, {})
        with patch("kovaaks.app._get_local_stats", return_value={}):
            app._rebuild_data()

        row = app._all_data[0]
        assert row["Potential"] != ""
        # Should be a numeric string
        int(row["Potential"])  # Should not raise

    def test_potential_targets_exclude_self_and_shrink_sparse_categories(self):
        info = {
            "a": {"name": "A", "entries": 1000, "aimType": "tracking"},
            "b": {"name": "B", "entries": 1000, "aimType": "clicking"},
            "c": {"name": "C", "entries": 1000, "aimType": "clicking"},
        }
        user = {lid: {"rank": rank, "score": 100} for lid, rank in (
            ("a", 10), ("b", 800), ("c", 400))}
        app = _make_app_stub(info, user, {})
        with patch("kovaaks.app._get_local_stats", return_value={}), patch(
                "kovaaks.app.calculate_potential_score",
                wraps=kovaaks_web.calculate_potential_score) as calculate:
            app._rebuild_data()

        targets = [call.kwargs["expected_pct"] for call in calculate.call_args_list]
        # A's 99th percentile cannot raise its own target: peers are 20 and 60.
        assert targets[0] == 40
        # B's one category peer (60) blends with five samples of global prior 79.5.
        assert targets[1] == 76.25

    def test_no_peers_uses_neutral_potential_target(self):
        app = _make_app_stub({"a": {"name": "A", "entries": 1000}},
                             {"a": {"rank": 800, "score": 100}}, {})
        with patch("kovaaks.app._get_local_stats", return_value={}):
            app._rebuild_data()
        assert app._all_data[0]["Potential"] == "300"
        # The existing global gain projection keeps its original category mean.
        assert app._global_projected_gain_sum == 0

    def test_invalid_user_rank_does_not_become_unplayed_opportunity_or_prior(self):
        info = {lid: {"name": lid, "entries": 1000} for lid in ("missing", "bad", "new")}
        user = {"missing": {"rank": None, "score": 100},
                "bad": {"rank": 1500, "score": 100}}
        app = _make_app_stub(info, user, {})
        with patch("kovaaks.app._get_local_stats", return_value={}):
            app._rebuild_data()
        rows = {row["Scenario"]: row for row in app._all_data}
        assert rows["missing"]["Potential"] == "0"
        assert rows["bad"]["Potential"] == "0"
        assert rows["new"]["Potential"] == "500"

    def test_row_potential_uses_observed_pb_evidence(self):
        app = _make_app_stub({"a": {"name": "A", "entries": 1000}},
                             {"a": {"rank": 800, "score": 100}}, {})
        stats = {"A": {"count": 41, "pb_observations": 41, "runs_since_pb": 40,
                       "recent_sample_count": 10, "trend": 1.0}}
        with patch("kovaaks.app._get_local_stats", return_value=stats):
            app._rebuild_data()
        assert 225 <= int(app._all_data[0]["Potential"]) < 300

    @pytest.mark.parametrize("rank,entries", [
        (True, 1000), (1.5, 1000), (1, True), (1, 1000.5),
    ])
    def test_nonintegral_or_boolean_evidence_does_not_raise_peer_targets(self, rank, entries):
        info = {"bad": {"name": "Bad", "entries": entries},
                "new": {"name": "New", "entries": 1000}}
        app = _make_app_stub(info, {"bad": {"rank": rank, "score": 100}}, {})
        with patch("kovaaks.app._get_local_stats", return_value={}):
            app._rebuild_data()
        rows = {row["Scenario"]: row for row in app._all_data}
        assert rows["Bad"]["Potential"] == "0"
        assert rows["New"]["Potential"] == "500"

    @patch('kovaaks.app.save_scores_cache')
    @patch('kovaaks.api.get_next_leaderboard_position_points')
    def test_get_next_rank_points(self, mock_get_points, mock_save_cache):
        class KovaaksAPIStub:
            def __init__(self):
                self._global_points_sum = 0
                self._cfg = {"username": ""}
                self._scores_cache = {}
                import threading
                self._shutdown_event = threading.Event()
                self._credentials_lock = threading.RLock()
                self._data_lock = threading.RLock()
                self._next_rank_lock = threading.Lock()
                self._next_rank_requests = {}
                self._credential_generation = 0
                self.window = None

            def _queue_cache_save(self):
                return mock_save_cache(self._scores_cache)
            
            get_next_rank_points = kovaaks_web.KovaaksAPI.get_next_rank_points

        app = KovaaksAPIStub()
        
        # 1. Zero points
        assert app.get_next_rank_points() == "N/A"
        
        # 2. No username
        app._global_points_sum = 5000
        assert app.get_next_rank_points() == "N/A (No Username)"
        
        # 3. Successful fetch with next points > current points
        app._cfg["username"] = "player1"
        mock_get_points.return_value = {"next_points": 5500, "user_official_points": 5000}
        assert app.get_next_rank_points() == "+500"
        mock_get_points.assert_called_with("player1", 5000)
        
        # 4. Rank 1 (next points <= current points)
        app._scores_cache.clear()
        mock_get_points.return_value = {"next_points": 4500, "user_official_points": 4500}
        assert app.get_next_rank_points() == "Rank 1!"
        
        # 5. Exception handled
        app._scores_cache.clear()
        mock_get_points.side_effect = Exception("API error")
        assert app.get_next_rank_points() == "Error"

    def test_get_scenarios_left_to_next_rank(self):
        class KovaaksAPIStub:
            def __init__(self):
                self._global_points_sum = 0
                self._cfg = {"username": ""}
                self._scores_cache = {}
                self._scenarios_expected_gains = []
                self._global_avg_pct = 75.0
                self._candidate_sum_entries = 1000
                self._candidate_sum_current_pts = 200
            
            get_scenarios_left_to_next_rank = kovaaks_web.KovaaksAPI.get_scenarios_left_to_next_rank

        app = KovaaksAPIStub()

        # 1. Zero points
        assert app.get_scenarios_left_to_next_rank() == {"count": "N/A", "live_gap": "N/A", "global_avg_pct": "N/A", "required_avg_pct": "N/A"}

        # 2. No username
        app._global_points_sum = 1000
        assert app.get_scenarios_left_to_next_rank() == {"count": "N/A", "live_gap": "N/A", "global_avg_pct": "N/A", "required_avg_pct": "N/A"}

        # 3. No cached next_rank / mismatched user
        app._cfg["username"] = "player1"
        assert app.get_scenarios_left_to_next_rank() == {"count": "N/A", "live_gap": "N/A", "global_avg_pct": "N/A", "required_avg_pct": "N/A"}

        app._scores_cache["next_rank"] = {
            "username": "different_player",
            "points": 1500
        }
        assert app.get_scenarios_left_to_next_rank() == {"count": "N/A", "live_gap": "N/A", "global_avg_pct": "N/A", "required_avg_pct": "N/A"}

        # 4. Cache exists but next points <= current points
        app._scores_cache["next_rank"] = {
            "username": "player1",
            "points": 1000
        }
        assert app.get_scenarios_left_to_next_rank() == {"count": "0", "live_gap": "+0", "global_avg_pct": "75.00%", "required_avg_pct": "0.00%"}

        # 5. Normal case where diff is met
        app._scores_cache["next_rank"]["points"] = 1500  # diff = 500
        app._scenarios_expected_gains = [300, 200, 100]
        assert app.get_scenarios_left_to_next_rank() == {"count": "2", "live_gap": "+500", "global_avg_pct": "75.00%", "required_avg_pct": "70.00%"}

        # 6. Diff met by exceeding
        app._scores_cache["next_rank"]["points"] = 1450  # diff = 450
        assert app.get_scenarios_left_to_next_rank() == {"count": "2", "live_gap": "+450", "global_avg_pct": "75.00%", "required_avg_pct": "65.00%"}

        # 7. All scenarios are not enough (required avg pct > 100%)
        app._scores_cache["next_rank"]["points"] = 2000  # diff = 1000
        assert app.get_scenarios_left_to_next_rank() == {"count": ">3", "live_gap": "+1,000", "global_avg_pct": "75.00%", "required_avg_pct": ">100%"}

        # 8. Exact match at 0 diff
        app._scores_cache["next_rank"]["points"] = 1000  # diff = 0
        assert app.get_scenarios_left_to_next_rank() == {"count": "0", "live_gap": "+0", "global_avg_pct": "75.00%", "required_avg_pct": "0.00%"}
