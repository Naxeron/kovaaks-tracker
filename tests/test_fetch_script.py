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
from scripts import fetch_scenarios
from scripts.fetch_scenarios import (
    fetch_all_scenarios as script_fetch_all,
    generate_datasets,
    merge_scenarios,
    update_history,
    write_gzip_json,
)


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

        mock_count.return_value = None  # Failed accurate lookup falls back to zero.

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

    def test_pagination_uses_original_counts_before_accurate_overwrite(self, monkeypatch):
        responses = [
            {"data": [{"leaderboardId": "first", "counts": {"entries": 5000}}], "total": 2},
            {"data": [{"leaderboardId": "second", "counts": {"entries": 3000}}], "total": 2},
        ]
        request = MagicMock(side_effect=[MagicMock(json=MagicMock(return_value=data)) for data in responses])
        count = MagicMock(side_effect=lambda lid, session: {"first": 1, "second": 2000}[lid])
        monkeypatch.setattr(fetch_scenarios, "api_request_with_retry", request)
        monkeypatch.setattr(fetch_scenarios, "get_accurate_entry_count", count)
        monkeypatch.setattr(fetch_scenarios.time, "sleep", MagicMock())

        result = script_fetch_all(entries_limit=1000)

        assert request.call_count == 2
        assert count.call_count == 2
        assert [item["counts"]["entries"] for item in result] == [1, 2000]

    def test_below_threshold_page_still_fetches_accurate_counts(self, monkeypatch):
        response = {"data": [{"leaderboardId": "first", "counts": {"entries": 50}, "scenario": {"counts": {"entries": 50}}}], "total": 2}
        request = MagicMock(return_value=MagicMock(json=MagicMock(return_value=response)))
        count = MagicMock(return_value=25)
        monkeypatch.setattr(fetch_scenarios, "api_request_with_retry", request)
        monkeypatch.setattr(fetch_scenarios, "get_accurate_entry_count", count)

        result = script_fetch_all(entries_limit=1000)

        assert request.call_count == 1
        count.assert_called_once()
        assert result[0]["counts"]["entries"] == 25
        assert result[0]["scenario"]["counts"]["entries"] == 25

    @pytest.mark.parametrize("previous_count, expected", [("77", 77), ("bad", 0)])
    def test_failed_accurate_lookup_uses_safe_existing_count(self, monkeypatch, previous_count, expected):
        response = {"data": [{"leaderboardId": "first", "counts": {"entries": 50000}}], "total": 1}
        monkeypatch.setattr(fetch_scenarios, "api_request_with_retry", MagicMock(return_value=MagicMock(json=MagicMock(return_value=response))))
        count = MagicMock(return_value=None)
        monkeypatch.setattr(fetch_scenarios, "get_accurate_entry_count", count)

        result = script_fetch_all(existing_scenarios={"first": scenario("first", previous_count)})

        count.assert_called_once()
        assert result[0]["counts"]["entries"] == expected

    def test_invalid_popularity_count_does_not_skip_accurate_lookup(self, monkeypatch):
        response = {"data": [{"leaderboardId": "first", "counts": {"entries": "bad"}}], "total": 1}
        monkeypatch.setattr(fetch_scenarios, "api_request_with_retry", MagicMock(return_value=MagicMock(json=MagicMock(return_value=response))))
        count = MagicMock(return_value=123)
        monkeypatch.setattr(fetch_scenarios, "get_accurate_entry_count", count)

        assert script_fetch_all()[0]["counts"]["entries"] == 123
        count.assert_called_once()


def scenario(lid, entries, name="Scenario"):
    return {"leaderboardId": lid, "scenarioName": name, "counts": {"entries": entries}}


def read_dataset(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


class TestScenarioMerging:
    """Exercise the same merge implementation used to generate published data."""

    def test_new_overwrites_existing(self):
        existing = {"lid-1": scenario("lid-1", 1000, "Old Name")}
        merged = merge_scenarios(existing, [
            scenario("lid-1", 2000, "New Name"), scenario("lid-2", 500, "Brand New")
        ])
        assert len(merged) == 2
        assert merged[0] == scenario("lid-1", 2000, "New Name")
        assert existing["lid-1"]["scenarioName"] == "Old Name"

    def test_merge_preserves_existing_not_in_new(self):
        merged = merge_scenarios(
            {"lid-old": scenario("lid-old", 100, "Ancient")},
            [scenario("lid-new", 999, "Fresh")],
        )
        assert [item["scenarioName"] for item in merged] == ["Fresh", "Ancient"]

    def test_stable_ties_and_invalid_counts(self):
        fetched = [scenario("lid-b", "100"), scenario("lid-a", 100), scenario("bad", "invalid")]
        forward = merge_scenarios({}, fetched)
        backward = merge_scenarios({}, list(reversed(fetched)))
        assert forward == backward
        assert [item["leaderboardId"] for item in forward] == ["lid-a", "lid-b", "bad"]

    def test_missing_ids_are_ignored(self):
        assert merge_scenarios({}, [{"counts": {"entries": 10}}]) == []


class TestHistoryTracking:
    """Exercise timestamp management and alignment in the publication code."""

    def test_replace_latest_within_hour(self):
        now = datetime.datetime(2026, 9, 30, 12, tzinfo=datetime.timezone.utc)
        recent = (now - datetime.timedelta(minutes=30)).replace(tzinfo=None).isoformat()
        original = {"timestamps": [recent], "history": {"lid-1": [3000]}}
        updated = update_history(original, [scenario("lid-1", 4000)], now)
        assert updated == {"timestamps": [recent], "history": {"lid-1": [4000]}}
        assert original["history"]["lid-1"] == [3000]

    def test_no_replace_after_hour_and_new_scenario_alignment(self):
        now = datetime.datetime(2026, 9, 30, 12)
        old = (now - datetime.timedelta(hours=2)).isoformat()
        updated = update_history(
            {"timestamps": [old], "history": {"lid-1": [3000], "gone": [10]}},
            [scenario("lid-1", 4000), scenario("new", 50)], now,
        )
        assert updated["timestamps"] == [old, now.isoformat()]
        assert updated["history"] == {"lid-1": [3000, 4000], "gone": [10, None], "new": [None, 50]}

    def test_prune_to_max_history(self):
        start = datetime.datetime(2026, 1, 1)
        timestamps = [(start + datetime.timedelta(hours=i)).isoformat() for i in range(200)]
        updated = update_history(
            {"timestamps": timestamps, "history": {"lid-1": list(range(200))}},
            [scenario("lid-1", 999)], start + datetime.timedelta(hours=200),
        )
        assert len(updated["timestamps"]) == 168
        assert updated["timestamps"][0] == timestamps[33]
        assert updated["history"]["lid-1"] == list(range(33, 200)) + [999]

    def test_timezone_aware_last_timestamp_uses_utc(self):
        updated = update_history(
            {"timestamps": ["2026-09-30T16:30:00+05:00"], "history": {"lid-1": [10]}},
            [scenario("lid-1", 20)], datetime.datetime(2026, 9, 30, 12, tzinfo=datetime.timezone.utc),
        )
        assert updated["timestamps"] == ["2026-09-30T16:30:00+05:00"]
        assert updated["history"]["lid-1"] == [20]

    def test_future_timestamp_does_not_replace_latest(self):
        updated = update_history(
            {"timestamps": ["2026-09-30T13:00:00"], "history": {"lid-1": [10]}},
            [scenario("lid-1", 20)], datetime.datetime(2026, 9, 30, 12),
        )
        assert len(updated["timestamps"]) == 2


class TestDeterministicWrites:
    def test_identical_payload_has_identical_gzip_bytes(self, tmp_path):
        first = tmp_path / "first" / "scenarios.json.gz"
        second = tmp_path / "second" / "different-name.json.gz"
        assert write_gzip_json(first, {"z": 1, "a": 2})
        assert write_gzip_json(second, {"a": 2, "z": 1})
        assert first.read_bytes() == second.read_bytes()
        assert first.read_bytes()[3] & 8 == 0  # No gzip filename field.
        assert first.read_bytes()[4:8] == b"\x00" * 4

    def test_unchanged_content_preserves_mtime_and_bytes(self, tmp_path):
        path = tmp_path / "scenarios.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            json.dump({"z": 1, "a": 2}, stream, indent=2)
        os.utime(path, ns=(1000000000, 2000000000))
        original = path.read_bytes()
        assert not write_gzip_json(path, {"a": 2, "z": 1})
        assert path.stat().st_mtime_ns == 2000000000
        assert path.read_bytes() == original

    def test_replace_failure_preserves_original_and_removes_temp(self, tmp_path, monkeypatch):
        path = tmp_path / "scenarios.json.gz"
        write_gzip_json(path, {"a": 1})
        original = path.read_bytes()
        replacement = MagicMock(side_effect=OSError("locked"))
        monkeypatch.setattr(fetch_scenarios.os, "replace", replacement)
        with pytest.raises(OSError, match="locked"):
            write_gzip_json(path, {"a": 2})
        assert path.read_bytes() == original
        assert list(tmp_path.iterdir()) == [path]
        assert str(os.getpid()) in replacement.call_args.args[0].name

    def test_corrupt_existing_file_is_not_overwritten(self, tmp_path):
        path = tmp_path / "scenarios.json.gz"
        path.write_bytes(b"corrupt")
        with pytest.raises(OSError):
            write_gzip_json(path, {"a": 2})
        assert path.read_bytes() == b"corrupt"


class TestGenerateDatasets:
    def test_output_directory_preserves_existing_scenarios_and_history(self, tmp_path, monkeypatch):
        output = tmp_path / "release-data"
        output.mkdir()
        scenarios_path = output / "scenarios.json.gz"
        history_path = output / "scenarios_history.json.gz"
        old = scenario("retained", 20)
        write_gzip_json(scenarios_path, [old])
        write_gzip_json(history_path, {"timestamps": ["2026-09-30T10:00:00"], "history": {"retained": [20]}})
        fetch = MagicMock(return_value=[scenario("new", 40)])
        monkeypatch.setattr(fetch_scenarios, "fetch_all_scenarios", fetch)
        assert generate_datasets(output, pages_limit=3, entries_limit=10, now=datetime.datetime(2026, 9, 30, 12))
        fetch.assert_called_once_with(pages_limit=3, entries_limit=10, existing_scenarios={"retained": old})
        assert read_dataset(scenarios_path) == [scenario("new", 40), old]
        assert read_dataset(history_path)["history"] == {"retained": [20, 20], "new": [None, 40]}

    def test_identical_fetch_in_same_hour_preserves_both_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fetch_scenarios, "fetch_all_scenarios", MagicMock(return_value=[scenario("lid-1", 10)]))
        assert generate_datasets(tmp_path, now=datetime.datetime(2026, 9, 30, 12))
        paths = [tmp_path / "scenarios.json.gz", tmp_path / "scenarios_history.json.gz"]
        originals = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        assert not generate_datasets(tmp_path, now=datetime.datetime(2026, 9, 30, 12, 30))
        assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == originals

    @pytest.mark.parametrize("fetched", [[], [{"counts": {"entries": 10}}]])
    def test_no_fetched_scenarios_preserves_existing_files(self, tmp_path, monkeypatch, fetched):
        paths = [tmp_path / "scenarios.json.gz", tmp_path / "scenarios_history.json.gz"]
        write_gzip_json(paths[0], [scenario("lid-1", 10)])
        write_gzip_json(paths[1], {"timestamps": ["2026-09-30T12:00:00"], "history": {"lid-1": [10]}})
        originals = {path: path.read_bytes() for path in paths}
        monkeypatch.setattr(fetch_scenarios, "fetch_all_scenarios", MagicMock(return_value=fetched))
        with pytest.raises(RuntimeError, match="No scenarios fetched"):
            generate_datasets(tmp_path)
        assert {path: path.read_bytes() for path in paths} == originals

    def test_empty_fetch_does_not_create_files(self, tmp_path, monkeypatch):
        output = tmp_path / "new"
        monkeypatch.setattr(fetch_scenarios, "fetch_all_scenarios", MagicMock(return_value=[]))
        with pytest.raises(RuntimeError):
            generate_datasets(output)
        assert not output.exists()

    @pytest.mark.parametrize("failure", ["no_response", "request_error", "invalid_json"])
    def test_failed_later_page_preserves_both_datasets(self, tmp_path, monkeypatch, failure):
        scenario_path = tmp_path / "scenarios.json.gz"
        history_path = tmp_path / "scenarios_history.json.gz"
        write_gzip_json(scenario_path, [scenario("first", 10), scenario("lower-page", 20)])
        write_gzip_json(history_path, {
            "timestamps": ["2026-09-30T10:00:00"], "history": {"first": [10], "lower-page": [20]},
        })
        originals = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (scenario_path, history_path)}
        first_page = MagicMock(json=MagicMock(return_value={
            "data": [scenario("first", 5000)], "total": 2,
        }))
        later_page = {
            "no_response": None,
            "request_error": OSError("offline"),
            "invalid_json": MagicMock(json=MagicMock(side_effect=ValueError("invalid JSON"))),
        }[failure]
        request = MagicMock(side_effect=[first_page, later_page])
        count = MagicMock(return_value=100)
        monkeypatch.setattr(fetch_scenarios, "api_request_with_retry", request)
        monkeypatch.setattr(fetch_scenarios, "get_accurate_entry_count", count)
        monkeypatch.setattr(fetch_scenarios.time, "sleep", MagicMock())

        with pytest.raises(RuntimeError, match="Failed to fetch scenario page 1"):
            generate_datasets(tmp_path, now=datetime.datetime(2026, 9, 30, 12))

        assert request.call_count == 2
        count.assert_called_once()
        assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in originals} == originals
        assert len(list(tmp_path.iterdir())) == 2

    @pytest.mark.parametrize("filename", ["scenarios.json.gz", "scenarios_history.json.gz"])
    @pytest.mark.parametrize("corruption", ["gzip", "json", "schema"])
    def test_corrupt_sources_abort_before_fetch_or_overwrite(self, tmp_path, monkeypatch, filename, corruption):
        scenario_path = tmp_path / "scenarios.json.gz"
        history_path = tmp_path / "scenarios_history.json.gz"
        write_gzip_json(scenario_path, [scenario("lid-1", 10)])
        write_gzip_json(history_path, {"timestamps": [], "history": {}})
        target = tmp_path / filename
        if corruption == "gzip":
            target.write_bytes(b"broken gzip")
        elif corruption == "json":
            target.write_bytes(gzip.compress(b"not JSON"))
        else:
            target.write_bytes(gzip.compress(b"null"))
        originals = {path: path.read_bytes() for path in (scenario_path, history_path)}
        fetch = MagicMock(return_value=[scenario("new", 30)])
        monkeypatch.setattr(fetch_scenarios, "fetch_all_scenarios", fetch)
        with pytest.raises((OSError, ValueError)):
            generate_datasets(tmp_path)
        fetch.assert_not_called()
        assert {path: path.read_bytes() for path in originals} == originals

    @pytest.mark.parametrize("history", [
        {"timestamps": ["bad timestamp"], "history": {"lid-1": [10]}},
        {"timestamps": [], "history": {"lid-1": [10]}},
        {"timestamps": [], "history": {"lid-1": "bad series"}},
    ])
    def test_invalid_history_structure_preserves_sources(self, tmp_path, monkeypatch, history):
        history_path = tmp_path / "scenarios_history.json.gz"
        write_gzip_json(history_path, history)
        original = history_path.read_bytes()
        fetch = MagicMock()
        monkeypatch.setattr(fetch_scenarios, "fetch_all_scenarios", fetch)
        with pytest.raises(ValueError, match="Invalid history"):
            generate_datasets(tmp_path)
        fetch.assert_not_called()
        assert history_path.read_bytes() == original

    def test_cli_passes_selected_output_directory(self, tmp_path, monkeypatch):
        generate = MagicMock()
        monkeypatch.setattr(fetch_scenarios, "generate_datasets", generate)
        assert fetch_scenarios.main(["--output-dir", str(tmp_path), "--min-entries", "10", "--pages", "2"]) == 0
        generate.assert_called_once_with(tmp_path, pages_limit=2, entries_limit=10)

    def test_cli_reports_failed_generation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fetch_scenarios, "generate_datasets", MagicMock(side_effect=RuntimeError("No scenarios fetched")))
        assert fetch_scenarios.main(["--output-dir", str(tmp_path)]) == 1

    @pytest.mark.parametrize("flag", ["--pages", "--min-entries"])
    def test_cli_rejects_negative_limits(self, tmp_path, monkeypatch, flag):
        generate = MagicMock()
        monkeypatch.setattr(fetch_scenarios, "generate_datasets", generate)
        with pytest.raises(SystemExit) as error:
            fetch_scenarios.main(["--output-dir", str(tmp_path), flag, "-1"])
        assert error.value.code == 2
        generate.assert_not_called()
