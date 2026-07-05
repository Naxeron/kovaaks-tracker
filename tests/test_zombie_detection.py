import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovaaks.api import is_scenario_zombie


def test_is_scenario_zombie_cached():
    # If in cached_zombies, should return True immediately
    res = is_scenario_zombie("Test Scenario", "/dummy/stats/", {"testscenario"})
    assert res is True


@patch("os.path.exists")
@patch("os.listdir")
def test_is_scenario_zombie_local_scenarios(mock_listdir, mock_exists):
    # Simulate scenario exists in local Saved/SaveGames/Scenarios folder
    mock_exists.side_effect = lambda path: "SaveGames/Scenarios" in path
    mock_listdir.return_value = ["1wall 6targets TE.sce", "Other.sce"]

    res = is_scenario_zombie("1wall 6targets TE", "/dummy/FPSAimTrainer/FPSAimTrainer/stats/", set())
    assert res is False


@patch("os.path.exists")
@patch("os.listdir")
def test_is_scenario_zombie_workshop(mock_listdir, mock_exists):
    # Simulate scenario exists in Steam Workshop folder
    mock_exists.side_effect = lambda path: "workshop/content/824270" in path
    mock_listdir.side_effect = lambda path: ["12345"] if path.endswith("824270") else ["Pasu Voltaic Easy.sce"]

    res = is_scenario_zombie("Pasu Voltaic Easy", "/dummy/FPSAimTrainer/FPSAimTrainer/stats/", set())
    assert res is False


@patch("os.path.exists")
@patch("requests.get")
def test_is_scenario_zombie_steam_workshop_search_found(mock_get, mock_exists):
    # Simulate not local on disk, query Steam Workshop, found
    mock_exists.return_value = False

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = 'dummy text \\\\\\\"title\\\\\\\":\\\\\\\"Pasu Voltaic Easy\\\\\\\" dummy'
    mock_get.return_value = mock_resp

    res = is_scenario_zombie("Pasu Voltaic Easy", "/dummy/stats/", set())
    assert res is False
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert "Pasu+Voltaic+Easy" in args[0]


@patch("os.path.exists")
@patch("requests.get")
def test_is_scenario_zombie_steam_workshop_search_zombie(mock_get, mock_exists):
    # Simulate not local, query Steam Workshop, NOT found (zombie)
    # mock_resp has total_count: 1, and 1 title ("Some Other Scenario").
    # Since total_count is small, it confirms it's a zombie.
    mock_exists.return_value = False

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = 'dummy text \\\\\\\"title\\\\\\\":\\\\\\\"Some Other Scenario\\\\\\\" \\\\\\\"total_count\\\\\\\":1'
    mock_get.return_value = mock_resp

    res = is_scenario_zombie("Pasu Voltaic Easy", "/dummy/stats/", set())
    assert res is True


@patch("os.path.exists")
@patch("requests.get")
def test_is_scenario_zombie_steam_workshop_search_common_name_not_zombie(mock_get, mock_exists):
    # Simulate not local, query Steam Workshop. The target scenario is not on the first page,
    # but total_count is large (e.g., 100), meaning there are more results.
    # It should NOT be flagged as a zombie (returns False).
    mock_exists.return_value = False

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = 'dummy text \\\\\\\"title\\\\\\\":\\\\\\\"Some Other Scenario\\\\\\\" \\\\\\\"total_count\\\\\\\":100'
    mock_get.value = mock_resp
    mock_get.return_value = mock_resp

    res = is_scenario_zombie("Pasu Voltaic Easy", "/dummy/stats/", set())
    assert res is False


@patch("os.path.exists")
@patch("requests.get")
def test_is_scenario_zombie_steam_workshop_search_error(mock_get, mock_exists):
    # Simulate connection error, should default to False (not zombie) to avoid false positives
    mock_exists.return_value = False
    mock_get.side_effect = Exception("Connection error")

    res = is_scenario_zombie("Pasu Voltaic Easy", "/dummy/stats/", set())
    assert res is False


@patch("os.path.exists")
@patch("requests.get")
def test_is_scenario_zombie_steam_workshop_search_no_items_found(mock_get, mock_exists):
    # Simulate not local, query Steam Workshop, returns "No items matching..."
    mock_exists.return_value = False

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = 'Some HTML... No items matching your search criteria were found ...'
    mock_get.return_value = mock_resp

    res = is_scenario_zombie("Pasu Voltaic Easy", "/dummy/stats/", set())
    assert res is True


@patch("os.path.exists")
@patch("requests.get")
def test_is_scenario_zombie_steam_workshop_paging_info_found(mock_get, mock_exists):
    # Simulate not local, query Steam Workshop, found in paging info
    mock_exists.return_value = False

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = 'HTML... \\\\\\\"title\\\\\\\":\\\\\\\"Pasu Voltaic Easy\\\\\\\" ... <div class="workshopBrowsePagingInfo">Showing 1-1 of 1 entries</div>'
    mock_get.return_value = mock_resp

    res = is_scenario_zombie("Pasu Voltaic Easy", "/dummy/stats/", set())
    assert res is False


@patch("os.path.exists")
@patch("requests.get")
def test_is_scenario_zombie_steam_workshop_paging_info_zombie(mock_get, mock_exists):
    # Simulate not local, query Steam Workshop, paging info found but title does not match
    mock_exists.return_value = False

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = 'HTML... \\\\\\\"title\\\\\\\":\\\\\\\"Some Other Scenario\\\\\\\" ... <div class="workshopBrowsePagingInfo">Showing 1-1 of 1 entries</div>'
    mock_get.return_value = mock_resp

    res = is_scenario_zombie("Pasu Voltaic Easy", "/dummy/stats/", set())
    assert res is True
