import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovaaks.fetch_worker import run_fetch_all

class TestFetchWorkerWorkItems:
    @patch("kovaaks.fetch_worker.fetch_gzip_json_from_github")
    @patch("kovaaks.fetch_worker.kovaaks_login")
    @patch("concurrent.futures.ThreadPoolExecutor")
    @patch("kovaaks.fetch_worker.save_scores_cache")
    def test_run_fetch_all_work_items_selection(self, mock_save, mock_executor, mock_login, mock_github):
        # 1. Setup mock app
        app = MagicMock()
        app._cfg = {"min_entries": 10}
        
        # Initial scores_cache state:
        # lid-1: not in scores_data at all -> must fetch
        # lid-2: in scores_data (unplayed) but in newly_played_scenarios -> must fetch
        # lid-3: in scores_data (unplayed) and has local runs but no user score -> must fetch
        # lid-4: in scores_data (unplayed), has NO local runs, not newly played -> do NOT fetch
        # lid-5: in scores_data (played), has local runs AND cached user score, not newly played -> do NOT fetch
        app._scores_cache = {
            "scenarios": [],
            "entry_history": {},
            "scores": {
                "lid-2": {},
                "lid-3": {},
                "lid-4": {},
                "lid-5": {"user": {"score": 100, "rank": 5}}
            },
            "newly_played_scenarios": ["Scen 2"],
            "local_stats": {
                "Scen 3": {"count": 3},
                "Scen 4": {"count": 0},
                "Scen 5": {"count": 5}
            }
        }
        
        # Return scenarios list from github mock
        mock_github.side_effect = [
            [
                {"leaderboardId": "lid-1", "scenarioName": "Scen 1", "counts": {"entries": 100}},
                {"leaderboardId": "lid-2", "scenarioName": "Scen 2", "counts": {"entries": 100}},
                {"leaderboardId": "lid-3", "scenarioName": "Scen 3", "counts": {"entries": 100}},
                {"leaderboardId": "lid-4", "scenarioName": "Scen 4", "counts": {"entries": 100}},
                {"leaderboardId": "lid-5", "scenarioName": "Scen 5", "counts": {"entries": 100}},
            ],
            None # scenarios_history.json.gz
        ]
        
        # Mock login to succeed
        mock_login.return_value = "fake-jwt-token"
        
        # Capture the work items sent to the executor
        captured_work_items = []
        
        class MockExecutorContext:
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
            def map(self, fn, items):
                nonlocal captured_work_items
                captured_work_items = list(items)
                
        mock_executor.side_effect = MockExecutorContext
        
        # Run fetch
        run_fetch_all(app, "test_user", "test_pass")
        
        # Verify work items
        assert "lid-1" in captured_work_items
        assert "lid-2" in captured_work_items
        assert "lid-3" in captured_work_items
        assert "lid-4" in captured_work_items
        assert "lid-5" in captured_work_items
        
        # "newly_played_scenarios" should have been popped/removed from scores_cache
        assert "newly_played_scenarios" not in app._scores_cache

    @patch("kovaaks.fetch_worker.fetch_gzip_json_from_github")
    @patch("kovaaks.fetch_worker.kovaaks_login")
    @patch("concurrent.futures.ThreadPoolExecutor")
    def test_run_fetch_all_respects_cancellation(self, mock_executor, mock_login, mock_github):
        app = MagicMock()
        app._cfg = {"min_entries": 10}
        app._scores_cache = {
            "scenarios": [],
            "entry_history": {},
            "scores": {}
        }
        
        # Set cancellation flag to True
        app._fetch_cancelled = True
        
        # Run fetch
        run_fetch_all(app, "test_user", "test_pass")
        
        # Verify early return: github, login, and executor should NOT be called/created
        mock_github.assert_not_called()
        mock_login.assert_not_called()
        mock_executor.assert_not_called()
        
        # Verify the cancellation handler was called on app
        app._rebuild_data_and_cancelled.assert_called_once()


class TestFetchWorkerAccurateCount:
    @patch("kovaaks.fetch_worker.fetch_gzip_json_from_github")
    @patch("kovaaks.fetch_worker.kovaaks_login")
    @patch("kovaaks.fetch_worker.kovaaks_get_friends_scores")
    @patch("kovaaks.api.get_accurate_entry_count")
    @patch("kovaaks.fetch_worker.save_scores_cache")
    def test_fetch_one_queries_accurate_count(self, mock_save, mock_get_acc, mock_get_friends, mock_login, mock_github):
        app = MagicMock()
        app._cfg = {"min_entries": 10}
        app._scores_cache = {
            "scenarios": [],
            "entry_history": {},
            "scores": {},
            "local_stats": {}
        }
        app._scenario_info = {"lid-1": {"name": "Scen 1", "entries": 100}}
        
        # Return scenarios list from github mock
        mock_github.side_effect = [
            [
                {"leaderboardId": "lid-1", "scenarioName": "Scen 1", "counts": {"entries": 100}},
            ],
            None
        ]
        
        # Mock login to succeed
        mock_login.return_value = "fake-jwt-token"
        
        # Mock friends scores to return user score
        mock_get_friends.return_value = [{"webappUsername": "test_user", "rank": 5, "score": 100}]
        
        # Mock accurate count to return 50
        mock_get_acc.return_value = 50
        
        # Run fetch
        run_fetch_all(app, "test_user", "test_pass")
        
        # Verify get_accurate_entry_count was called
        assert mock_get_acc.call_count == 1
        args, kwargs = mock_get_acc.call_args
        assert args[0] == "lid-1"
        
        # Verify entries was updated in scenario_info and scores cache
        assert app._scenario_info["lid-1"]["entries"] == 50
        assert app._scores_cache["scores"]["lid-1"]["entries"] == 50
