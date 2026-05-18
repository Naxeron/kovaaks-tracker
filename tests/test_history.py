"""
Tests for _record_history_points and history management.
"""
import datetime
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kovaaks_gui


def _make_app_stub_for_history(entry_history=None):
    """Create a minimal mock of KovaaksApp for history testing."""
    app = MagicMock()
    app._scores_cache = {"entry_history": entry_history or {}}
    app._record_history_points = kovaaks_gui.KovaaksApp._record_history_points.__get__(app)
    return app


class TestRecordHistoryPoints:
    def test_records_new_lid(self):
        app = _make_app_stub_for_history()
        scenarios = [{"leaderboardId": "lid-1", "counts": {"entries": 5000}}]
        app._record_history_points(scenarios)

        history = app._scores_cache["entry_history"]
        assert "lid-1" in history
        assert len(history["lid-1"]) == 1
        # The value should be the entry count
        assert list(history["lid-1"].values())[0] == 5000

    def test_adds_new_timestamp(self):
        """Running again after an hour should create a new timestamp."""
        old_time = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                    - datetime.timedelta(hours=2))
        old_key = old_time.isoformat()

        app = _make_app_stub_for_history({"lid-1": {old_key: 4000}})
        scenarios = [{"leaderboardId": "lid-1", "counts": {"entries": 5000}}]
        app._record_history_points(scenarios)

        history = app._scores_cache["entry_history"]["lid-1"]
        assert len(history) == 2
        assert old_key in history
        assert history[old_key] == 4000

    def test_deduplicates_within_hour(self):
        """Calls within the same hour should update the existing timestamp."""
        recent_time = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                       - datetime.timedelta(minutes=30))
        recent_key = recent_time.isoformat()

        app = _make_app_stub_for_history({"lid-1": {recent_key: 4000}})
        scenarios = [{"leaderboardId": "lid-1", "counts": {"entries": 5500}}]
        app._record_history_points(scenarios)

        history = app._scores_cache["entry_history"]["lid-1"]
        assert len(history) == 1  # Should not have created a new key
        assert history[recent_key] == 5500  # Updated value

    def test_prunes_to_168(self):
        """History should prune to exactly 168 entries via while loop.
        
        With 170 entries + 1 new (not deduped), the code adds the new one (171)
        then prunes down to 168.  Entries are spaced 2 hours apart so dedup
        doesn't trigger.
        """
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        # Create 170 entries, each spaced 2 hours apart (beyond 1-hour dedup window)
        old_history = {}
        for i in range(170):
            dt = now - datetime.timedelta(hours=(170 - i) * 2)
            old_history[dt.isoformat()] = 1000 + i

        app = _make_app_stub_for_history({"lid-1": old_history})
        scenarios = [{"leaderboardId": "lid-1", "counts": {"entries": 9999}}]
        app._record_history_points(scenarios)

        history = app._scores_cache["entry_history"]["lid-1"]
        # After adding one new entry (171) and pruning to 168
        assert len(history) == 168

    def test_cleans_future_timestamps(self):
        """Timestamps in the future (>1 hour) should be cleaned up."""
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        future = now + datetime.timedelta(hours=5)
        past = now - datetime.timedelta(hours=3)

        app = _make_app_stub_for_history({
            "lid-1": {
                future.isoformat(): 9999,
                past.isoformat(): 3000,
            }
        })
        scenarios = [{"leaderboardId": "lid-1", "counts": {"entries": 5000}}]
        app._record_history_points(scenarios)

        history = app._scores_cache["entry_history"]["lid-1"]
        # Future timestamp should have been removed
        assert future.isoformat() not in history
        # Past timestamp should remain
        assert past.isoformat() in history

    def test_handles_invalid_entry_count(self):
        """Non-integer entry counts should be skipped."""
        app = _make_app_stub_for_history()
        scenarios = [{"leaderboardId": "lid-bad", "counts": {"entries": "not_a_number"}}]
        app._record_history_points(scenarios)

        history = app._scores_cache["entry_history"]
        # Should not have created an entry
        assert "lid-bad" not in history

    def test_multiple_scenarios(self):
        app = _make_app_stub_for_history()
        scenarios = [
            {"leaderboardId": "lid-1", "counts": {"entries": 1000}},
            {"leaderboardId": "lid-2", "counts": {"entries": 2000}},
            {"leaderboardId": "lid-3", "counts": {"entries": 3000}},
        ]
        app._record_history_points(scenarios)

        history = app._scores_cache["entry_history"]
        assert len(history) == 3
        for lid in ["lid-1", "lid-2", "lid-3"]:
            assert lid in history
