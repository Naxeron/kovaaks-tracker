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
        assert "lid-4" not in captured_work_items
        assert "lid-5" not in captured_work_items
        
        # "newly_played_scenarios" should have been popped/removed from scores_cache
        assert "newly_played_scenarios" not in app._scores_cache
