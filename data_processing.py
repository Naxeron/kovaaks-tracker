"""
KovaaKs Scenario Tracker — data processing utilities.

Estimation functions, sorting helpers, and shared score-parsing logic.
"""

import datetime
import logging

from constants import (
    SCENARIO_DISTRIBUTION_POINTS,
    SCENARIO_POPULARITY_DROP_OFF_POINTS,
)

logger = logging.getLogger("kovaaks")


# ---------------------------------------------------------------------------
# Estimation / interpolation
# ---------------------------------------------------------------------------

def get_estimated_fetch_count(min_entries):
    """Estimate how many items we need to pull from the 'popular' API
    before hitting the min_entries threshold.
    """
    m = min_entries
    if m <= 0:
        return SCENARIO_POPULARITY_DROP_OFF_POINTS[0][1]
    if m >= SCENARIO_POPULARITY_DROP_OFF_POINTS[-1][0]:
        return 0

    for i in range(len(SCENARIO_POPULARITY_DROP_OFF_POINTS) - 1):
        x1, y1 = SCENARIO_POPULARITY_DROP_OFF_POINTS[i]
        x2, y2 = SCENARIO_POPULARITY_DROP_OFF_POINTS[i + 1]
        if x1 <= m <= x2:
            ratio = (m - x1) / (x2 - x1)
            return y1 - ratio * (y1 - y2)
    return 0


def get_estimated_matching_count(min_entries):
    """Estimate how many scenarios will actually match the min_entries threshold."""
    m = min_entries
    if m <= 0:
        return SCENARIO_DISTRIBUTION_POINTS[0][1]
    if m >= SCENARIO_DISTRIBUTION_POINTS[-1][0]:
        return 0

    for i in range(len(SCENARIO_DISTRIBUTION_POINTS) - 1):
        x1, y1 = SCENARIO_DISTRIBUTION_POINTS[i]
        x2, y2 = SCENARIO_DISTRIBUTION_POINTS[i + 1]
        if x1 <= m <= x2:
            ratio = (m - x1) / (x2 - x1)
            return y1 - ratio * (y1 - y2)
    return 0


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
    # Try to clean up numeric strings like "1,234" or "-47.20%"
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
    """Parse a friends-leaderboard API response into user + friend entries.

    Returns:
        tuple: (user_entry_dict_or_None, list_of_friend_dicts)
    """
    friend_entries = []
    user_entry = None

    for entry in data:
        name = (entry.get("webappUsername")
                or entry.get("steamAccountName", ""))
        epoch = entry.get("attributes", {}).get("epoch", "")
        score_date = ""
        if epoch:
            try:
                score_date = datetime.datetime.fromtimestamp(
                    int(epoch) / 1000
                ).strftime("%Y-%m-%d")
            except (ValueError, TypeError, OSError):
                pass

        if name.lower() == username.lower():
            user_entry = {
                "rank": entry.get("rank", ""),
                "score": entry.get("score", ""),
                "date": score_date,
            }
        else:
            friend_entries.append({
                "friend": name,
                "rank": entry.get("rank", ""),
                "score": entry.get("score", ""),
                "date": score_date,
            })

    return user_entry, friend_entries
