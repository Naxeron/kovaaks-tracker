"""
KovaaKs Scenario Tracker — shared constants.

Colors, column definitions, distribution curves, URLs, and other
project-wide constants live here so every module can import them
without circular dependencies.
"""

# ---------------------------------------------------------------------------
# Defaults and thresholds
# ---------------------------------------------------------------------------
MIN_ENTRIES = 1000


# ---------------------------------------------------------------------------
# Distribution curves for estimation
# ---------------------------------------------------------------------------

# Distribution of scenarios by entry count (actual count of scenarios with >= X entries)
SCENARIO_DISTRIBUTION_POINTS = [
    (0, 18000), (10, 17953), (50, 17420), (100, 13772),
    (500, 6639), (1000, 4852), (2000, 3458), (5000, 1960),
    (10000, 1162), (100000, 0)
]

# Estimated number of items to fetch from the 'popular' endpoint
# until the entry count drops consistently below X.
# This curve is different because popularity != entry count.
SCENARIO_POPULARITY_DROP_OFF_POINTS = [
    (0, 18000), (10, 17500), (50, 15000), (100, 12000),
    (500, 9000), (1000, 6500), (2000, 5000), (5000, 3500),
    (10000, 2000), (20000, 1000), (50000, 400), (100000, 0)
]

# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/Naxeron/kovaaks-tracker/main/data"

# Steam launch URI for KovaaKs (App ID 824270)
STEAM_LAUNCH_URI = "steam://run/824270/?action=jump-to-scenario;name={}"

# ---------------------------------------------------------------------------
# Log trimming
# ---------------------------------------------------------------------------
LAUNCH_MARKER = "=" * 60 + " LAUNCH " + "=" * 60
