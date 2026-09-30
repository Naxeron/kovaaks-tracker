"""Regression tests for scenario pagination and accurate entry counts."""

from unittest.mock import MagicMock

import pytest

from kovaaks import api


def _scenario(lid, entries):
    return {
        "leaderboardId": lid,
        "counts": {"entries": entries},
        "scenario": {"counts": {"entries": entries}},
    }


def _mock_fetch(monkeypatch, pages, accurate_counts):
    responses = []
    for page in pages:
        response = MagicMock()
        response.json.return_value = page
        responses.append(response)
    request = MagicMock(side_effect=responses)
    count = MagicMock(side_effect=lambda lid, session: accurate_counts[lid])
    monkeypatch.setattr(api, "api_request_with_retry", request)
    monkeypatch.setattr(api, "get_accurate_entry_count", count)
    monkeypatch.setattr(api.time, "sleep", lambda _: None)
    return request, count


def test_pagination_keeps_later_qualifying_scenario(monkeypatch):
    """A low accurate first-page count must not stop popularity pagination."""
    request, count = _mock_fetch(monkeypatch, [
        {"data": [_scenario("first", 500000)], "total": 2},
        {"data": [_scenario("second", 400000)], "total": 2},
    ], {"first": 10, "second": 2000})

    scenarios = api.fetch_all_scenarios(min_entries=1000, session=MagicMock())

    assert [call.kwargs["params"]["page"] for call in request.call_args_list] == [0, 1]
    assert {call.args[0] for call in count.call_args_list} == {"first", "second"}
    assert {item["leaderboardId"]: item["counts"]["entries"] for item in scenarios} == {
        "first": 10, "second": 2000,
    }
    assert [item["leaderboardId"] for item in scenarios
            if item["counts"]["entries"] >= 1000] == ["second"]
    assert [item["scenario"]["counts"]["entries"] for item in scenarios] == [10, 2000]


def test_pagination_stops_at_low_original_popularity_count(monkeypatch):
    request, count = _mock_fetch(monkeypatch, [
        {"data": [_scenario("first", "999")], "total": 2},
    ], {"first": 5000})

    scenarios = api.fetch_all_scenarios(min_entries=1000, session=MagicMock())

    request.assert_called_once()
    count.assert_called_once()
    assert scenarios[0]["counts"]["entries"] == 5000


@pytest.mark.parametrize("original_count", [None, "invalid"])
def test_pagination_handles_invalid_popularity_counts(monkeypatch, original_count):
    request, _ = _mock_fetch(monkeypatch, [
        {"data": [_scenario("first", original_count)], "total": 2},
    ], {"first": 1500})

    scenarios = api.fetch_all_scenarios(min_entries=1000, session=MagicMock())

    request.assert_called_once()
    assert scenarios[0]["counts"]["entries"] == 1500


def test_zero_threshold_fetches_all_pages(monkeypatch):
    request, _ = _mock_fetch(monkeypatch, [
        {"data": [_scenario("first", 0)], "total": 2},
        {"data": [_scenario("second", 0)], "total": 2},
    ], {"first": 0, "second": 0})

    scenarios = api.fetch_all_scenarios(min_entries=0, session=MagicMock())

    assert len(scenarios) == 2
    assert request.call_count == 2
