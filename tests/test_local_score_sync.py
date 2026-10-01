"""Local score synchronization persists only changed leaderboard information."""

from copy import deepcopy
import threading
from unittest.mock import Mock

import pytest

import kovaaks.api as remote_api
from kovaaks import app as kovaaks_web


@pytest.fixture
def sync(monkeypatch, tmp_path):
    # Exercise the event handler without starting loaders, watchers, or a GUI.
    api = kovaaks_web.KovaaksAPI.__new__(kovaaks_web.KovaaksAPI)
    api._shutdown_event = threading.Event()
    api._credentials_lock = threading.RLock()
    api._data_lock = threading.RLock()
    api._credentials_loaded_event = threading.Event()
    api._credentials_loaded_event.set()
    api._cfg = {"username": "player"}
    api._password = ""
    api._credential_generation = 1
    api._jwt_token = "existing-session"
    api._scenario_info = {"one": {"name": "Scenario A"}}
    user = {"rank": 10, "score": 100, "date": ""}
    friends = [{"friend": "friend", "rank": 2, "score": 200, "date": ""}]
    api._scores_cache = {"scores": {"one": {"user": user, "friends": friends}}}
    api._user_by_lid = {"one": user}
    api._friends_by_lid = {"one": friends}
    api._local_stats_dirty = False
    api._queue_cache_save = Mock()
    api.window = Mock()
    filename = "Scenario A - Challenge - 2026.09.30-12.00.00 Stats.csv"
    (tmp_path / filename).write_text("Score:,100\n", encoding="utf-8")
    response = [{"webappUsername": "player", "rank": 10, "score": 100},
                {"webappUsername": "friend", "rank": 2, "score": 200}]
    request = Mock(return_value=response)
    monkeypatch.setattr(remote_api, "kovaaks_get_friends_scores", request)
    monkeypatch.setattr(kovaaks_web.time, "sleep", Mock())
    info = Mock()
    monkeypatch.setattr(kovaaks_web.logger, "info", info)
    return api, request, info, tmp_path, filename


def refresh_count(api):
    return sum(call.args == ("if(window.fetchData) window.fetchData()",)
               for call in api.window.evaluate_js.call_args_list)


def test_identical_scores_keep_cached_objects_without_remote_refresh_or_save(sync):
    api, request, info, directory, filename = sync
    old_user = api._scores_cache["scores"]["one"]["user"]
    old_friends = api._scores_cache["scores"]["one"]["friends"]

    api._handle_new_stats_files(str(directory), [filename])

    request.assert_called_once()
    api._queue_cache_save.assert_not_called()
    info.assert_not_called()
    # Both local-file refreshes remain; no third refresh for an unchanged API score.
    assert refresh_count(api) == 2
    assert api._scores_cache["scores"]["one"]["user"] is old_user
    assert api._scores_cache["scores"]["one"]["friends"] is old_friends
    assert api._user_by_lid["one"] is old_user
    assert api._friends_by_lid["one"] is old_friends


@pytest.mark.parametrize("change", ["user", "friends", "both"])
def test_changed_score_information_refreshes_and_saves_once(sync, change):
    api, request, info, directory, filename = sync
    response = deepcopy(request.return_value)
    if change in ("user", "both"):
        response[0]["score"] = 150
    if change in ("friends", "both"):
        response[1]["score"] = 250
    request.return_value = response

    api._handle_new_stats_files(str(directory), [filename])

    request.assert_called_once()
    api._queue_cache_save.assert_called_once_with()
    assert refresh_count(api) == 3
    assert api._user_by_lid["one"]["score"] == (150 if change in ("user", "both") else 100)
    assert api._friends_by_lid["one"][0]["score"] == (250 if change in ("friends", "both") else 200)
    if change == "friends":
        info.assert_not_called()
    else:
        info.assert_called_once_with("Auto-updated score for %s", "Scenario A")


def test_changed_friends_without_user_preserve_cached_user_and_are_saved(sync):
    api, request, _, directory, filename = sync
    old_user = api._scores_cache["scores"]["one"]["user"]
    request.return_value = [{"webappUsername": "friend", "rank": 2, "score": 250}]

    api._handle_new_stats_files(str(directory), [filename])

    # Preserve the existing delayed-user retry policy before accepting friends.
    assert request.call_count == 5
    api._queue_cache_save.assert_called_once_with()
    assert refresh_count(api) == 3
    assert api._scores_cache["scores"]["one"]["user"] is old_user
    assert api._friends_by_lid["one"][0]["score"] == 250


@pytest.mark.parametrize("response", [[], [{"webappUsername": "player", "rank": 10, "score": 100}]])
def test_absent_entries_do_not_erase_cached_user_or_friends(sync, response):
    api, request, info, directory, filename = sync
    before = deepcopy(api._scores_cache)
    request.return_value = response

    api._handle_new_stats_files(str(directory), [filename])

    assert api._scores_cache == before
    api._queue_cache_save.assert_not_called()
    info.assert_not_called()
    assert refresh_count(api) == 2


def test_changed_response_from_previous_account_is_discarded(sync):
    api, request, info, directory, filename = sync
    before = deepcopy(api._scores_cache)

    def change_account(*args, **kwargs):
        api._credential_generation += 1
        return [{"webappUsername": "player", "rank": 1, "score": 900}]

    request.side_effect = change_account

    api._handle_new_stats_files(str(directory), [filename])

    assert api._scores_cache == before
    api._queue_cache_save.assert_not_called()
    info.assert_not_called()
    assert refresh_count(api) == 2


def test_shutdown_during_later_sync_keeps_accepted_scores_dirty(sync):
    """Shutdown can persist earlier responses even if this batch never finishes."""
    api, request, _, directory, filename = sync
    second_filename = "Scenario B - Challenge - 2026.09.30-12.00.00 Stats.csv"
    (directory / second_filename).write_text("Score:,100\n", encoding="utf-8")
    api._scenario_info["two"] = {"name": "Scenario B"}

    def fetch(token, lid, **kwargs):
        if lid == "one":
            return [{"webappUsername": "player", "rank": 1, "score": 150}]
        assert api._scores_cache["_dirty"] is True
        api._shutdown_event.set()
        raise remote_api.RequestCancelled("Fetch cancelled")

    request.side_effect = fetch
    api._handle_new_stats_files(str(directory), [filename, second_filename])

    assert api._scores_cache["scores"]["one"]["user"]["score"] == 150
    assert api._scores_cache["_dirty"] is True
    api._queue_cache_save.assert_not_called()
