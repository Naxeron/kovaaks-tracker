"""
Tests for the filter logic in _apply_filter (data-level, not GUI).

We test the filtering algorithm by directly manipulating _all_data rows
and calling filter functions without the GUI layer.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_row(scenario, my_rank="", best_friend="", rank_diff="", **extra):
    """Build a minimal row dict matching _rebuild_data output."""
    row = {
        "Scenario": scenario,
        "Entry Count": extra.get("entry_count", "5000"),
        "My Rank": my_rank,
        "My Score": extra.get("my_score", ""),
        "Percentile": extra.get("percentile", ""),
        "Score Date": extra.get("score_date", ""),
        "Best Friend": best_friend,
        "Friend Rank": extra.get("friend_rank", ""),
        "Friend Score": extra.get("friend_score", ""),
        "Friend Percentile": extra.get("friend_percentile", ""),
        "Friend Score Date": extra.get("friend_score_date", ""),
        "Rank Diff": rank_diff,
        "Pctile Diff": extra.get("pctile_diff", ""),
        "Local Runs": extra.get("local_runs", "0"),
        "Potential": extra.get("potential", ""),
        "New Entries (24h)": extra.get("new_entries", "0"),
        "Trend Mult": extra.get("trend_mult", "0.20x"),
    }
    return row


def _apply_filter_logic(all_rows, losing=False, friends_only=False,
                        me_only=False, unplayed=False, query=""):
    """Replicate the filter logic from KovaaksApp._apply_filter without GUI.

    Uses set-based accumulation: each active toggle adds matching row indices
    to a set, then we take the union. This allows multiple filters to stack.
    """
    if losing or friends_only or me_only or unplayed:
        matched = set()
        for idx, r in enumerate(all_rows):
            has_rank = r.get("My Rank", "") != ""
            has_friend = r.get("Best Friend", "") != ""
            rank_diff = r.get("Rank Diff", "")

            if losing and has_rank and has_friend and rank_diff:
                try:
                    if int(rank_diff) > 0:
                        matched.add(idx)
                except (ValueError, TypeError):
                    pass
            if friends_only and has_friend and not has_rank:
                matched.add(idx)
            if me_only and has_rank and not has_friend:
                matched.add(idx)
            if unplayed and not has_rank and not has_friend:
                matched.add(idx)
        all_rows = [all_rows[i] for i in sorted(matched)]

    if query:
        query = query.lower()
        all_rows = [r for r in all_rows if any(
            query in str(v).lower() for v in r.values())]

    return all_rows


class TestFilterLogic:
    def _build_dataset(self):
        return [
            # User + Friend, losing (rank_diff > 0 → user is worse)
            _make_row("ScenA", my_rank="200", best_friend="Alice", rank_diff="100"),
            # User + Friend, winning (rank_diff < 0)
            _make_row("ScenB", my_rank="50", best_friend="Bob", rank_diff="-50"),
            # Friends only (no user rank)
            _make_row("ScenC", best_friend="Charlie"),
            # Me only (no friend)
            _make_row("ScenD", my_rank="300"),
            # Unplayed (no user, no friend)
            _make_row("ScenE"),
            # Another unplayed
            _make_row("ScenF"),
        ]

    def test_no_filter(self):
        rows = self._build_dataset()
        result = _apply_filter_logic(rows)
        assert len(result) == 6

    def test_losing_filter(self):
        rows = self._build_dataset()
        result = _apply_filter_logic(rows, losing=True)
        assert len(result) == 1
        assert result[0]["Scenario"] == "ScenA"

    def test_friends_only_filter(self):
        rows = self._build_dataset()
        result = _apply_filter_logic(rows, friends_only=True)
        assert len(result) == 1
        assert result[0]["Scenario"] == "ScenC"

    def test_me_only_filter(self):
        rows = self._build_dataset()
        result = _apply_filter_logic(rows, me_only=True)
        assert len(result) == 1
        assert result[0]["Scenario"] == "ScenD"

    def test_unplayed_filter(self):
        rows = self._build_dataset()
        result = _apply_filter_logic(rows, unplayed=True)
        assert len(result) == 2
        names = {r["Scenario"] for r in result}
        assert names == {"ScenE", "ScenF"}

    def test_text_filter(self):
        rows = self._build_dataset()
        result = _apply_filter_logic(rows, query="ScenA")
        assert len(result) == 1
        assert result[0]["Scenario"] == "ScenA"

    def test_text_filter_case_insensitive(self):
        rows = self._build_dataset()
        result = _apply_filter_logic(rows, query="scena")
        assert len(result) == 1

    def test_text_filter_matches_any_field(self):
        rows = self._build_dataset()
        result = _apply_filter_logic(rows, query="Alice")
        assert len(result) == 1
        assert result[0]["Scenario"] == "ScenA"

    def test_combined_toggle_and_text(self):
        """Toggle filter then text filter should stack."""
        rows = self._build_dataset()
        result = _apply_filter_logic(rows, unplayed=True, query="ScenE")
        assert len(result) == 1
        assert result[0]["Scenario"] == "ScenE"

    def test_no_matches(self):
        rows = self._build_dataset()
        result = _apply_filter_logic(rows, query="nonexistent")
        assert len(result) == 0

    # --- New tests for set-based accumulation ---

    def test_combined_toggles_stack(self):
        """Multiple toggle filters should return the UNION of matches."""
        rows = self._build_dataset()
        result = _apply_filter_logic(rows, me_only=True, unplayed=True)
        assert len(result) == 3
        names = {r["Scenario"] for r in result}
        assert names == {"ScenD", "ScenE", "ScenF"}

    def test_all_toggles_active(self):
        """All toggles active should return the union of all categories."""
        rows = self._build_dataset()
        result = _apply_filter_logic(rows, losing=True, friends_only=True,
                                     me_only=True, unplayed=True)
        # ScenA (losing), ScenC (friends_only), ScenD (me_only), ScenE+F (unplayed)
        # ScenB is winning so not matched by any toggle
        assert len(result) == 5
        excluded = {r["Scenario"] for r in result}
        assert "ScenB" not in excluded

    def test_losing_and_friends_only(self):
        """Losing + Friends Only should return union."""
        rows = self._build_dataset()
        result = _apply_filter_logic(rows, losing=True, friends_only=True)
        assert len(result) == 2
        names = {r["Scenario"] for r in result}
        assert names == {"ScenA", "ScenC"}

    def test_order_preserved(self):
        """Results should maintain original row order."""
        rows = self._build_dataset()
        result = _apply_filter_logic(rows, me_only=True, unplayed=True)
        scenarios = [r["Scenario"] for r in result]
        assert scenarios == ["ScenD", "ScenE", "ScenF"]
