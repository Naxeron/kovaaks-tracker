"""
Shared fixtures for KovaaKs Tracker test suite.
"""
import datetime
import json
import os
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Sample scenario data
# ---------------------------------------------------------------------------

def _make_scenario(lid, name, entries, **extra):
    """Build a minimal scenario dict matching the API/cache schema."""
    s = {
        "leaderboardId": lid,
        "scenarioName": name,
        "counts": {"entries": entries},
    }
    s.update(extra)
    return s


@pytest.fixture
def sample_scenarios():
    """A small list of realistic scenario dicts."""
    return [
        _make_scenario("lid-1", "1w6ts Reload", 12000),
        _make_scenario("lid-2", "Pasu Voltaic Easy", 8500),
        _make_scenario("lid-3", "Bounceshot", 3000),
        _make_scenario("lid-4", "Air Angelic 4 Voltaic", 950),   # below 1000
        _make_scenario("lid-5", "Close Long Strafes Invincible", 6000),
    ]


@pytest.fixture
def sample_user_scores():
    """User score entries keyed by leaderboard ID."""
    return {
        "lid-1": {"rank": 150, "score": 1234.5, "date": "2026-05-10"},
        "lid-2": {"rank": 420, "score": 987.0, "date": "2026-05-12"},
        "lid-5": {"rank": 30, "score": 5500.0, "date": "2026-05-15"},
    }


@pytest.fixture
def sample_friend_scores():
    """Friend score entries keyed by leaderboard ID."""
    return {
        "lid-1": [
            {"friend": "Alice", "rank": 100, "score": 1500.0, "date": "2026-05-09"},
            {"friend": "Bob", "rank": 300, "score": 1100.0, "date": "2026-05-08"},
        ],
        "lid-2": [
            {"friend": "Alice", "rank": 200, "score": 1050.0, "date": "2026-05-11"},
        ],
        "lid-3": [
            {"friend": "Charlie", "rank": 500, "score": 800.0, "date": "2026-05-14"},
        ],
    }


@pytest.fixture
def sample_cache(sample_scenarios, sample_user_scores, sample_friend_scores):
    """A fully-populated scores_cache dict matching the on-disk format."""
    scores = {}
    for lid, user in sample_user_scores.items():
        scores[lid] = {"user": user}
    for lid, friends in sample_friend_scores.items():
        scores.setdefault(lid, {})["friends"] = friends
    return {
        "scenarios": sample_scenarios,
        "scores": scores,
        "entry_history": {},
    }


# ---------------------------------------------------------------------------
# Stats directory fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def stats_dir(tmp_path):
    """Create a temporary stats directory with sample CSV files."""
    stats = tmp_path / "stats"
    stats.mkdir()

    now = datetime.datetime.now()

    # Create multiple stats files for the same scenario (for trend calculation)
    for i in range(5):
        dt = now - datetime.timedelta(hours=i * 2)
        date_str = dt.strftime("%Y.%m.%d-%H.%M.%S")
        score = 1000.0 + i * 10  # Improving scores over time (older = higher index = lower score)
        fname = f"1w6ts Reload - Challenge - {date_str} Stats.csv"
        (stats / fname).write_text(
            f"Scenario:,1w6ts Reload\n"
            f"Score:,{score}\n"
            f"Kills:,50\n",
            encoding="utf-8",
        )

    # Single run for another scenario
    dt = now - datetime.timedelta(days=3)
    date_str = dt.strftime("%Y.%m.%d-%H.%M.%S")
    fname = f"Pasu Voltaic Easy - Challenge - {date_str} Stats.csv"
    (stats / fname).write_text(
        "Scenario:,Pasu Voltaic Easy\nScore:,850.5\n",
        encoding="utf-8",
    )

    return str(stats)


# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def config_dir(tmp_path):
    """Provide a temp directory for config files."""
    return tmp_path


@pytest.fixture
def sample_config():
    """A realistic config dict."""
    return {
        "username": "testuser",
        "stats_dir": "/fake/stats",
        "min_entries": "1000",
        "auto_refresh": False,
        "refresh_interval": "60",
        "always_show_total_points": True,
        "visible_columns": ["Entry Count", "My Rank", "My Score"],
    }


# ---------------------------------------------------------------------------
# API response fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_login_response():
    """Successful login response payload."""
    return {
        "auth": {
            "jwt": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.fakepayload.fakesig",
            "otherField": "value",
        }
    }


@pytest.fixture
def mock_friends_response():
    """Friends leaderboard response for a single scenario."""
    return [
        {
            "webappUsername": "testuser",
            "steamAccountName": "testuser_steam",
            "rank": 150,
            "score": 1234.5,
            "attributes": {"epoch": "1747000000000"},
        },
        {
            "webappUsername": "friend1",
            "steamAccountName": "friend1_steam",
            "rank": 100,
            "score": 1500.0,
            "attributes": {"epoch": "1746900000000"},
        },
        {
            "webappUsername": "",
            "steamAccountName": "friend2_steam",
            "rank": 300,
            "score": 1100.0,
            "attributes": {"epoch": "1746800000000"},
        },
    ]


@pytest.fixture
def mock_popular_page():
    """A single page response from the scenarios/popular endpoint."""
    return {
        "data": [
            _make_scenario("lid-a", "Scenario A", 5000),
            _make_scenario("lid-b", "Scenario B", 3000),
        ],
        "total": 2,
    }
