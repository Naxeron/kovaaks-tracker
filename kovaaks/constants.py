"""
KovaaKs Scenario Tracker — shared constants.

Colors, column definitions, distribution curves, URLs, and other
project-wide constants live here so every module can import them
without circular dependencies.
"""

# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------
BG          = "#1a1a2e"
BG_DARKER   = "#16162a"
BG_LIGHTER  = "#222240"
ACCENT      = "#e94560"
ACCENT_HOVER= "#ff6b81"
TEXT        = "#eaeaea"
TEXT_DIM    = "#999"
ENTRY_BG    = "#2a2a4a"
TREE_BG     = "#1e1e38"
TREE_FG     = "#dcdcdc"
TREE_SEL_BG = "#e94560"
TREE_SEL_FG = "#ffffff"
LOG_BG      = "#12122a"
LOG_FG      = "#8888aa"
ALT_ROW     = "#24243e"
HEADER_BG   = "#2e2e50"
BORDER      = "#3a3a5c"
GREEN       = "#2ecc71"
GREEN_HOVER = "#27ae60"

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------
COLUMNS = [
    ("▶", 32),
    ("Scenario", 240),
    ("Entry Count", 80),
    ("New Entries (24h)", 120),
    ("Trend Mult", 80),
    ("My Rank", 70),
    ("My Score", 85),
    ("Percentile", 80),
    ("Score Date", 95),
    ("Best Friend", 130),
    ("Friend Rank", 80),
    ("Friend Score", 90),
    ("Friend Percentile", 95),
    ("Friend Score Date", 105),
    ("Rank Diff", 80),
    ("Pctile Diff", 80),
    ("Local Runs", 80),
    ("Potential", 70),
]

# Columns to auto-hide when a specific filter is active
FILTER_HIDDEN_COLS = {
    "friends_only": {"My Rank", "My Score", "Percentile", "Score Date",
                     "Rank Diff", "Pctile Diff"},
    "me_only":      {"Best Friend", "Friend Rank", "Friend Score",
                     "Friend Percentile", "Friend Score Date",
                     "Rank Diff", "Pctile Diff"},
    "unplayed":     {"My Rank", "My Score", "Percentile", "Score Date",
                     "Best Friend", "Friend Rank", "Friend Score",
                     "Friend Percentile", "Friend Score Date",
                     "Rank Diff", "Pctile Diff", "Local Runs", "Potential"},
}

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
