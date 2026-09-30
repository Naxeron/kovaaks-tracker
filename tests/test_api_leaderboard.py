import pytest
from unittest.mock import patch, MagicMock
import kovaaks.api as api

@patch('kovaaks.api.api_request_with_retry')
def test_get_next_leaderboard_position_points_found_user(mock_req):
    # Mock finding the user on the first page
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": [
            {"webappUsername": "player1", "points": 5000},
            {"webappUsername": "testuser", "points": 4000},
            {"webappUsername": "player3", "points": 3000}
        ]
    }
    mock_req.return_value = mock_resp
    
    # Should return points of player1 (5000) and user_official_points (4000)
    res = api.get_next_leaderboard_position_points("testuser", 4000)
    assert res == {"next_points": 5000, "user_official_points": 4000}

@patch('kovaaks.api.api_request_with_retry')
def test_get_next_leaderboard_position_points_user_rank_1(mock_req):
    # Mock user is rank 1
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": [
            {"webappUsername": "testuser", "points": 5000},
            {"webappUsername": "player2", "points": 4000}
        ]
    }
    mock_req.return_value = mock_resp
    
    # Should return local_points since they are rank 1
    res = api.get_next_leaderboard_position_points("testuser", 5000)
    assert res == {"next_points": 5000, "user_official_points": 5000}

@patch('kovaaks.api.api_request_with_retry')
def test_get_next_leaderboard_position_points_binary_search(mock_req):
    # User not in top 100
    
    def side_effect(method, url, params, session=None, **kwargs):
        mock_resp = MagicMock()
        page = params.get("page", 0)
        
        if page == 0:
            mock_resp.json.return_value = {
                "total": 500,
                "data": [{"webappUsername": f"p{i}", "points": 5000 - i*10} for i in range(100)] # 5000 to 4010
            }
        elif page == 1:
            mock_resp.json.return_value = {
                "total": 500,
                "data": [{"webappUsername": f"p{i}", "points": 4000 - i*10} for i in range(100)] # 4000 to 3010
            }
        elif page == 2:
            mock_resp.json.return_value = {
                "total": 500,
                "data": [{"webappUsername": f"p{i}", "points": 3000 - i*10} for i in range(100)] # 3000 to 2010
            }
        elif page == 3:
            mock_resp.json.return_value = {
                "total": 500,
                "data": [{"webappUsername": f"p{i}", "points": 2000 - i*10} for i in range(100)] # 2000 to 1010
            }
        elif page == 4:
            mock_resp.json.return_value = {
                "total": 500,
                "data": [{"webappUsername": f"p{i}", "points": 1000 - i*10} for i in range(100)] # 1000 to 10
            }
        else:
            mock_resp.json.return_value = {"total": 500, "data": []}
            
        return mock_resp
        
    mock_req.side_effect = side_effect
    
    # Target points: 2505
    # The minimum points strictly greater than 2505 should be 2510.
    # It falls in page 2.
    res = api.get_next_leaderboard_position_points("testuser", 2505)
    assert res == {"next_points": 2510, "user_official_points": None}

    # Target points: 3005
    # Minimum strictly greater is 3010 (from page 1)
    res = api.get_next_leaderboard_position_points("testuser", 3005)
    assert res == {"next_points": 3010, "user_official_points": None}
    
    # Target points: 50
    # Minimum strictly greater is 60 (from page 4)
    res = api.get_next_leaderboard_position_points("testuser", 50)
    assert res == {"next_points": 60, "user_official_points": None}

@patch('kovaaks.api.api_request_with_retry')
def test_get_next_leaderboard_position_points_error(mock_req):
    # Simulate API exception
    mock_req.side_effect = Exception("API rate limited or down")
    
    with pytest.raises(Exception, match="API rate limited or down"):
        api.get_next_leaderboard_position_points("testuser", 4000)


@patch('kovaaks.api.api_request_with_retry')
def test_binary_search_fetches_each_page_once(mock_req):
    def response_for_page(method, url, params, **kwargs):
        page = params["page"]
        response = MagicMock()
        response.json.return_value = {
            "total": 500,
            "data": [{"webappUsername": f"p{page}-{i}", "points": 5000 - page * 1000 - i * 10}
                     for i in range(100)],
        }
        return response

    mock_req.side_effect = response_for_page
    session = MagicMock()

    result = api.get_next_leaderboard_position_points("testuser", 2505, session=session)

    assert result == {"next_points": 2510, "user_official_points": None}
    assert [call.kwargs["params"]["page"] for call in mock_req.call_args_list] == [0, 2]
    assert all(call.kwargs["session"] is session for call in mock_req.call_args_list)


@patch('kovaaks.api.api_request_with_retry')
def test_page_boundary_reuses_previous_page(mock_req):
    first = MagicMock()
    first.json.return_value = {"total": 200, "data": [
        {"webappUsername": f"p{i}", "points": 5000 - i * 10} for i in range(100)
    ]}
    second = MagicMock()
    second.json.return_value = {"total": 200, "data": [
        {"webappUsername": "testuser", "points": 4000}
    ]}
    mock_req.side_effect = [first, second]

    assert api.get_next_leaderboard_position_points("testuser", 4000) == {
        "next_points": 4010, "user_official_points": 4000,
    }
    assert [call.kwargs["params"]["page"] for call in mock_req.call_args_list] == [0, 1]


@pytest.mark.parametrize("failure", [None, RuntimeError("request failed"), "invalid_json"])
@patch('kovaaks.api.api_request_with_retry')
def test_unsuccessful_first_page_is_retried(mock_req, failure):
    if failure == "invalid_json":
        failure = MagicMock()
        failure.json.side_effect = ValueError("invalid JSON")
    success = MagicMock()
    success.json.return_value = {"total": 0, "data": []}
    mock_req.side_effect = [failure, success]

    assert api.get_next_leaderboard_position_points("testuser", 4000) == {
        "next_points": 4000, "user_official_points": None,
    }
    assert [call.kwargs["params"]["page"] for call in mock_req.call_args_list] == [0, 0]


@patch('kovaaks.api.api_request_with_retry')
def test_page_cache_does_not_outlive_lookup(mock_req):
    responses = []
    for points in (4000, 5000):
        response = MagicMock()
        response.json.return_value = {"data": [{"webappUsername": "testuser", "points": points}]}
        responses.append(response)
    mock_req.side_effect = responses

    assert api.get_next_leaderboard_position_points("testuser", 4000)["next_points"] == 4000
    assert api.get_next_leaderboard_position_points("testuser", 4000)["next_points"] == 5000
    assert mock_req.call_count == 2
