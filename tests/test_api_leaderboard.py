import pytest
from unittest.mock import patch, MagicMock
import api

@patch('api.api_request_with_retry')
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
    
    # Should return points of player1 (5000)
    res = api.get_next_leaderboard_position_points("testuser", 4000)
    assert res == 5000

@patch('api.api_request_with_retry')
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
    assert res == 5000

@patch('api.api_request_with_retry')
def test_get_next_leaderboard_position_points_binary_search(mock_req):
    # User not in top 100
    
    def side_effect(method, url, params, session=None):
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
    assert res == 2510

    # Target points: 3005
    # Minimum strictly greater is 3010 (from page 1)
    res = api.get_next_leaderboard_position_points("testuser", 3005)
    assert res == 3010
    
    # Target points: 50
    # Minimum strictly greater is 60 (from page 4)
    res = api.get_next_leaderboard_position_points("testuser", 50)
    assert res == 60
