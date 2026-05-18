"""
Tests for api_request_with_retry network helper and API functions.
"""
import sys
import os
from unittest.mock import patch, MagicMock, PropertyMock
import time

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import (
    api_request_with_retry,
    get_accurate_entry_count,
    kovaaks_login,
    kovaaks_get_friends_scores,
)


# ---------------------------------------------------------------------------
# api_request_with_retry
# ---------------------------------------------------------------------------

class TestApiRequestWithRetry:
    def _mock_response(self, status_code=200, json_data=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data or {}
        resp.raise_for_status.side_effect = (
            None if status_code < 400
            else requests.exceptions.HTTPError(response=resp)
        )
        return resp

    @patch("api.requests.get")
    def test_success_on_first_try(self, mock_get):
        mock_get.return_value = self._mock_response(200, {"ok": True})
        result = api_request_with_retry("get", "http://example.com", max_retries=3)
        assert result.status_code == 200
        assert mock_get.call_count == 1

    @patch("api.time.sleep")
    @patch("api.requests.get")
    def test_retries_on_500(self, mock_get, mock_sleep):
        """Should retry on 5xx errors."""
        fail_resp = self._mock_response(500)
        ok_resp = self._mock_response(200, {"ok": True})
        mock_get.side_effect = [fail_resp, ok_resp]

        result = api_request_with_retry("get", "http://example.com", max_retries=3)
        assert result.status_code == 200
        assert mock_get.call_count == 2

    @patch("api.requests.get")
    def test_raises_on_4xx_immediately(self, mock_get):
        """4xx errors (except 429) should not be retried."""
        mock_get.return_value = self._mock_response(403)
        with pytest.raises(requests.exceptions.HTTPError):
            api_request_with_retry("get", "http://example.com", max_retries=3)
        assert mock_get.call_count == 1

    @patch("api.time.sleep")
    @patch("api.requests.get")
    def test_retries_on_connection_error(self, mock_get, mock_sleep):
        """Should retry on ConnectionError."""
        mock_get.side_effect = [
            requests.exceptions.ConnectionError("conn refused"),
            self._mock_response(200),
        ]
        result = api_request_with_retry("get", "http://example.com", max_retries=3)
        assert result.status_code == 200
        assert mock_get.call_count == 2

    @patch("api.time.sleep")
    @patch("api.requests.get")
    def test_retries_on_timeout(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            requests.exceptions.Timeout("timed out"),
            self._mock_response(200),
        ]
        result = api_request_with_retry("get", "http://example.com", max_retries=3)
        assert result.status_code == 200

    @patch("api.time.sleep")
    @patch("api.requests.get")
    def test_max_retries_exhausted_raises(self, mock_get, mock_sleep):
        """After max_retries, the last exception should propagate."""
        mock_get.side_effect = requests.exceptions.ConnectionError("down")
        with pytest.raises(requests.exceptions.ConnectionError):
            api_request_with_retry("get", "http://example.com", max_retries=2)
        assert mock_get.call_count == 3  # initial + 2 retries

    @patch("api.requests.get")
    def test_uses_session_when_provided(self, mock_requests_get):
        session = MagicMock()
        session.get.return_value = self._mock_response(200)
        api_request_with_retry("get", "http://example.com", session=session)
        session.get.assert_called_once()
        mock_requests_get.assert_not_called()

    @patch("api.time.sleep")
    @patch("api.requests.post")
    def test_post_method(self, mock_post, mock_sleep):
        mock_post.return_value = self._mock_response(200)
        result = api_request_with_retry("post", "http://example.com", max_retries=1)
        assert result.status_code == 200
        mock_post.assert_called_once()


# ---------------------------------------------------------------------------
# get_accurate_entry_count
# ---------------------------------------------------------------------------

class TestGetAccurateEntryCount:
    @patch("api.api_request_with_retry")
    def test_returns_count(self, mock_req):
        resp = MagicMock()
        resp.json.return_value = {"total": 5000}
        mock_req.return_value = resp
        result = get_accurate_entry_count("lid-123")
        assert result == 5000

    @patch("api.api_request_with_retry")
    def test_returns_none_on_failure(self, mock_req):
        mock_req.side_effect = Exception("network error")
        result = get_accurate_entry_count("lid-bad")
        assert result is None

    @patch("api.api_request_with_retry")
    def test_returns_none_when_no_response(self, mock_req):
        mock_req.return_value = None
        result = get_accurate_entry_count("lid-none")
        assert result is None


# ---------------------------------------------------------------------------
# kovaaks_login
# ---------------------------------------------------------------------------

class TestKovaaksLogin:
    @patch("api.api_request_with_retry")
    def test_extracts_jwt(self, mock_req, mock_login_response):
        resp = MagicMock()
        resp.json.return_value = mock_login_response
        mock_req.return_value = resp
        token = kovaaks_login("user", "pass")
        assert token.startswith("eyJ")

    @patch("api.api_request_with_retry")
    def test_raises_on_missing_jwt(self, mock_req):
        resp = MagicMock()
        resp.json.return_value = {"auth": {"noJwt": "here"}}
        mock_req.return_value = resp
        with pytest.raises(ValueError, match="Could not find JWT"):
            kovaaks_login("user", "pass")


# ---------------------------------------------------------------------------
# kovaaks_get_friends_scores
# ---------------------------------------------------------------------------

class TestKovaaksGetFriendsScores:
    @patch("api.api_request_with_retry")
    def test_returns_data_list(self, mock_req, mock_friends_response):
        resp = MagicMock()
        resp.json.return_value = {"data": mock_friends_response}
        mock_req.return_value = resp
        result = kovaaks_get_friends_scores("fake-token", "lid-1")
        assert len(result) == 3
        assert result[0]["rank"] == 150
