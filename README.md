# ⌖ KovaaKs Scenario Tracker

A vibe coded tracker for KovaaKs, primarily built for sandbox rank farming and sniping friend scores.

## Issues and contributions

I may be slow to respond to issues and contributions, depends on how I'm feeling and my Antigravity quota. Feel free to open issues regardless.

## Features

- **Scenario Browser** — View all popular scenarios (configurable min. leaderboard entries), sorted and filterable
- **Live Score Tracking** — Fetches your rank, score, and percentile from the KovaaKs API
- **Friend Comparison** — See your best friend's score side-by-side with rank/percentile diffs
- **Potential Score** — Smart practice recommendation algorithm factoring in skill gap, spaced repetition, session fatigue, plateau detection, and competition trends
- **Local Stats Integration** — Reads your Steam stats directory to track local run counts and score trends
- **Auto-Update** — Polls your stats folder and refreshes scores automatically when you finish a run
- **Autoplay Mode** — Automatically launches the next scenario in your list after completing one
- **One-Click Launch** — Start any scenario directly via `steam://` URI
- **Filter Modes** — Toggle between Losing, Friends Only, Me Only, and Unplayed views
- **Column Customization** — Right-click to show/hide columns; auto-fits on resize
- **Persistent Cache** — Scores and scenarios are cached locally for instant startup

## Requirements

- Python 3.10+
- A KovaaKs account (username & password)

## Installation

```bash
git clone https://github.com/naxeron/kovaaks-tracker.git
cd kovaaks-tracker
pip install requests
python kovaaks_gui.py
```

> **Note:** Tkinter ships with most Python installations. If missing, install it via your package manager (e.g. `sudo apt install python3-tk`).

## Usage

1. **Launch** — Run `python kovaaks_gui.py`
2. **Configure** — Click ⚙ to enter your KovaaKs username and password
3. **Refresh** — Click ⟳ to fetch scenarios and scores (first run takes a few minutes)
4. **Browse** — Sort by any column, filter by name, or use toggle filters
5. **Play** — Click ▶ on any row to launch the scenario in KovaaKs via Steam
6. **Autoplay** — Enable 🔁 to auto-advance through your list after each run

## How Potential Score Works

The Potential column ranks scenarios by practice value using:

| Factor | Description |
|---|---|
| **Skill Gap** | Logarithmic potential based on your rank vs. total entries |
| **Spaced Repetition** | Ebbinghaus-inspired decay — scenarios you haven't played recently get priority |
| **Session Fatigue** | Exponential decay for scenarios played many times today |
| **Plateau Detection** | Sigmoid penalty when your score hasn't improved over recent runs |
| **Trend Bonus** | Boosts scenarios where you're actively improving |
| **Competition** | Scales by daily new entries — busier leaderboards get weighted higher |

## Keybindings

| Key | Action |
|---|---|
| Double-click row | Copy scenario name to clipboard |
| Enter | Launch selected scenario |
| Right-click table | Toggle column visibility |

