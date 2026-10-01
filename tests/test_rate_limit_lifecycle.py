"""Rate limiting and cancellation must preserve previously fetched scores."""

from copy import deepcopy
from unittest.mock import Mock

import requests

from kovaaks import api as http_api
from kovaaks import fetch_worker
from kovaaks import app as kovaaks_web


def make_api(monkeypatch, tmp_path):
    monkeypatch.setattr(kovaaks_web, "load_config", lambda: {
        "username": "player", "stats_dir": str(tmp_path), "min_entries": 10,
    })
    monkeypatch.setattr(kovaaks_web, "load_scores_cache", lambda: {
        "scenarios": [{"leaderboardId": "one", "scenarioName": "Scenario A", "counts": {"entries": 1000}}],
        "scores": {"one": {"user": {"rank": 10, "score": 100, "date": ""}}},
    })
    monkeypatch.setattr(kovaaks_web.time, "sleep", lambda _: None)
    return kovaaks_web.KovaaksAPI()


def test_auto_sync_does_not_restart_exhausted_rate_limit_retries(monkeypatch, tmp_path):
    app = make_api(monkeypatch, tmp_path)
    app._jwt_token = "token"
    before = deepcopy(app._scores_cache["scores"])
    run = tmp_path / "Scenario A - Challenge - 2026.09.30-12.00.00 Stats.csv"
    run.write_text("Score:,120\n", encoding="utf-8")
    response = requests.Response()
    response.status_code = 429
    fetch = Mock(side_effect=requests.HTTPError(response=response))
    monkeypatch.setattr(http_api, "kovaaks_get_friends_scores", fetch)

    app._handle_new_stats_files(str(tmp_path), {run.name})

    fetch.assert_called_once()
    assert app._scores_cache["scores"] == before
    assert fetch.call_args.kwargs["cancel_check"]() is False
    app._credential_generation += 1
    assert fetch.call_args.kwargs["cancel_check"]() is True


def test_cancelling_a_paced_bulk_request_preserves_scores(monkeypatch, tmp_path):
    app = make_api(monkeypatch, tmp_path)
    before = deepcopy(app._scores_cache["scores"])
    monkeypatch.setattr(fetch_worker, "fetch_gzip_json_from_github", lambda *_: fetch_worker.DATASET_UNCHANGED)
    monkeypatch.setattr(fetch_worker, "kovaaks_login", lambda *_, **__: "token")
    app._rebuild_data_and_cancelled = Mock()

    def cancel(token, lid, session, cancel_check):
        app._fetch_cancelled = True
        assert cancel_check() is True
        raise http_api.RequestCancelled("Fetch cancelled")

    fetch = Mock(side_effect=cancel)
    monkeypatch.setattr(fetch_worker, "kovaaks_get_friends_scores", fetch)

    fetch_worker.run_fetch_all(app, "player", "password")

    fetch.assert_called_once()
    assert app._scores_cache["scores"] == before
    assert app._user_by_lid["one"]["score"] == 100
    assert app._fetch_in_progress is False
    app._rebuild_data_and_cancelled.assert_called_once_with(silent=False)
