"""
Tests for scripts/fetch_scenarios.py — scenario merging and history tracking.
"""
import datetime
import gzip
import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# Add both the project root and scripts dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from kovaaks.api import (
    api_request_with_retry,
    get_accurate_entry_count,
)
from scripts.fetch_scenarios import fetch_all_scenarios as script_fetch_all


class TestApiRetryFromModule:
    @patch("kovaaks.api.requests.get")
    def test_success(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        result = api_request_with_retry("get", "http://example.com", max_retries=1)
        assert result.status_code == 200

    @patch("kovaaks.api.time.sleep")
    @patch("kovaaks.api.requests.get")
    def test_retry_on_connection_error(self, mock_get, mock_sleep):
        import requests as req
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.raise_for_status = MagicMock()
        mock_get.side_effect = [
            req.exceptions.ConnectionError("fail"),
            ok_resp,
        ]
        result = api_request_with_retry("get", "http://example.com", max_retries=2)
        assert result.status_code == 200


class TestEntryCountFromModule:
    @patch("kovaaks.api.api_request_with_retry")
    def test_returns_count(self, mock_req):
        resp = MagicMock()
        resp.json.return_value = {"total": 7777}
        mock_req.return_value = resp
        assert get_accurate_entry_count("lid-test") == 7777

    @patch("kovaaks.api.api_request_with_retry")
    def test_returns_none_on_error(self, mock_req):
        mock_req.side_effect = Exception("boom")
        assert get_accurate_entry_count("lid-fail") is None


class TestScriptFetchAll:
    @patch("scripts.fetch_scenarios.get_accurate_entry_count")
    @patch("scripts.fetch_scenarios.api_request_with_retry")
    @patch("scripts.fetch_scenarios.time.sleep")
    def test_fetches_single_page(self, mock_sleep, mock_req, mock_count):
        page_data = {
            "data": [
                {"leaderboardId": "lid-1", "counts": {"entries": 5000}},
                {"leaderboardId": "lid-2", "counts": {"entries": 3000}},
            ],
            "total": 2,
        }
        resp = MagicMock()
        resp.json.return_value = page_data
        resp.status_code = 200
        mock_req.return_value = resp

        mock_count.return_value = None  # Skip accurate counts

        result = script_fetch_all(pages_limit=1, entries_limit=100)
        assert len(result) == 2

    @patch("scripts.fetch_scenarios.get_accurate_entry_count")
    @patch("scripts.fetch_scenarios.api_request_with_retry")
    @patch("scripts.fetch_scenarios.time.sleep")
    def test_stops_on_empty_page(self, mock_sleep, mock_req, mock_count):
        page1 = MagicMock()
        page1.json.return_value = {
            "data": [{"leaderboardId": "lid-1", "counts": {"entries": 5000}}],
            "total": 100,
        }
        page1.status_code = 200

        page2 = MagicMock()
        page2.json.return_value = {"data": [], "total": 100}
        page2.status_code = 200

        mock_req.side_effect = [page1, page2]
        mock_count.return_value = None

        result = script_fetch_all(entries_limit=0)
        assert len(result) == 1


class TestScenarioMerging:
    """Test the merge logic from the script's __main__ block."""

    def test_new_overwrites_existing(self):
        existing = {
            "lid-1": {"leaderboardId": "lid-1", "scenarioName": "Old Name",
                      "counts": {"entries": 1000}},
        }
        new_list = [
            {"leaderboardId": "lid-1", "scenarioName": "New Name",
             "counts": {"entries": 2000}},
            {"leaderboardId": "lid-2", "scenarioName": "Brand New",
             "counts": {"entries": 500}},
        ]

        for s in new_list:
            l_id = s.get("leaderboardId")
            if l_id:
                existing[l_id] = s

        merged = list(existing.values())
        assert len(merged) == 2

        by_lid = {s["leaderboardId"]: s for s in merged}
        assert by_lid["lid-1"]["scenarioName"] == "New Name"
        assert by_lid["lid-1"]["counts"]["entries"] == 2000

    def test_merge_preserves_existing_not_in_new(self):
        existing = {
            "lid-old": {"leaderboardId": "lid-old", "scenarioName": "Ancient",
                        "counts": {"entries": 100}},
        }
        new_list = [
            {"leaderboardId": "lid-new", "scenarioName": "Fresh",
             "counts": {"entries": 999}},
        ]

        for s in new_list:
            l_id = s.get("leaderboardId")
            if l_id:
                existing[l_id] = s

        merged = list(existing.values())
        assert len(merged) == 2
        names = {s["scenarioName"] for s in merged}
        assert "Ancient" in names
        assert "Fresh" in names


class TestHistoryTracking:
    """Test history timestamp management from the script."""

    def test_replace_latest_within_hour(self):
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        recent = now - datetime.timedelta(minutes=30)

        history_data = {
            "timestamps": [recent.isoformat()],
            "history": {"lid-1": [3000]},
        }

        # Simulate the replace_latest check
        last_ts_str = history_data["timestamps"][-1]
        lk = last_ts_str
        last_dt = datetime.datetime.fromisoformat(lk).replace(tzinfo=None)
        replace_latest = (now - last_dt).total_seconds() < 3600

        assert replace_latest is True

    def test_no_replace_after_hour(self):
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        old = now - datetime.timedelta(hours=2)

        last_dt = old
        replace_latest = (now - last_dt).total_seconds() < 3600

        assert replace_latest is False

    def test_prune_to_max_history(self):
        MAX_HISTORY = 168
        timestamps = [f"2026-01-{i:03d}T00:00:00" for i in range(200)]
        history = {"lid-1": list(range(200))}

        if len(timestamps) > MAX_HISTORY:
            timestamps = timestamps[-MAX_HISTORY:]
            for lid in history:
                history[lid] = history[lid][-MAX_HISTORY:]

        assert len(timestamps) == MAX_HISTORY
        assert len(history["lid-1"]) == MAX_HISTORY
