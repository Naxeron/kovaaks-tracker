"""
KovaaKs Scenario Tracker — data processing utilities.

Estimation functions, sorting helpers, and shared score-parsing logic.
"""

import datetime
import logging

from .constants import (
    SCENARIO_DISTRIBUTION_POINTS,
    SCENARIO_POPULARITY_DROP_OFF_POINTS,
)

logger = logging.getLogger("kovaaks")


# ---------------------------------------------------------------------------
# Estimation / interpolation
# ---------------------------------------------------------------------------

def safe_int(v, default=0):
    try: return int(v)
    except (ValueError, TypeError): return default


def safe_float(v, default=0.0):
    try: return float(v)
    except (ValueError, TypeError): return default


def _interpolate(m, points):
    if m <= 0:
        return points[0][1]
    if m >= points[-1][0]:
        return 0
    for i in range(len(points) - 1):
        x1, y1 = points[i]
        x2, y2 = points[i + 1]
        if x1 <= m <= x2:
            return y1 - (m - x1) / (x2 - x1) * (y1 - y2)
    return 0


def get_estimated_fetch_count(min_entries):
    """Estimate how many items we need to pull from the 'popular' API."""
    return _interpolate(min_entries, SCENARIO_POPULARITY_DROP_OFF_POINTS)


def get_estimated_matching_count(min_entries):
    """Estimate how many scenarios will actually match the min_entries threshold."""
    return _interpolate(min_entries, SCENARIO_DISTRIBUTION_POINTS)


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------

def natural_sort_key(val):
    """Sort key that handles numbers, percentages, comma-separated numbers,
    and plain strings in a natural order.
    """
    s = str(val).strip()
    if not s:
        return (2, "")
    clean = s.replace(",", "")
    if clean.endswith("%"):
        clean = clean[:-1]

    try:
        return (0, float(clean))
    except (ValueError, TypeError):
        return (1, s.lower())


# ---------------------------------------------------------------------------
# Score parsing  (shared between _do_fetch_all and _handle_new_stats_files)
# ---------------------------------------------------------------------------

def parse_leaderboard_entries(data, username):
    """Parse a friends-leaderboard API response into user + friend entries."""
    friend_entries, user_entry = [], None
    for entry in data:
        name = entry.get("webappUsername") or entry.get("steamAccountName", "")
        epoch = entry.get("attributes", {}).get("epoch", "")
        try:
            score_date = datetime.datetime.fromtimestamp(int(epoch) / 1000).strftime("%Y-%m-%d") if epoch else ""
        except (ValueError, TypeError, OSError):
            score_date = ""
        item = {"rank": entry.get("rank", ""), "score": entry.get("score", ""), "date": score_date}
        if name.lower() == username.lower():
            user_entry = item
        else:
            friend_entries.append({"friend": name, **item})
    return user_entry, friend_entries

