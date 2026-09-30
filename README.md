# ⌖ KovaaKs Scenario Tracker

A vibe-coded tracker for KovaaKs. Built for rank farming, sniping friends, and optimizing your practice.

![KovaaKs Scenario Tracker Screenshot](screenshot_v2.png)

## Features

- **Rank Tracking** — Live updates via KovaaKs API with accurate stats (unlike the official website).
- **Friend Sniping** — Compare scores side-by-side.
- **Potential Score** — Smart practice algorithm recommending scenarios based on skill gap, fatigue, and trends.
- **Autoplay** — Automatically launches the next scenario after you finish a run.

## Setup

1. **Clone & Install**:
   ```bash
   git clone https://github.com/naxeron/kovaaks-tracker.git
   cd kovaaks-tracker
   pip install -r requirements.txt
   ```
   *Note for Linux users (especially Arch Linux) running the Web UI*:
   `pywebview` requires system-level GUI/WebKit backends. For the forced GTK backend on Arch Linux, run:
   ```bash
   sudo pacman -S python-gobject webkit2gtk-4.1
   ```
   For Debian/Ubuntu-based systems, run:
   ```bash
   sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
   ```

2. **Run**:
   ```bash
   python kovaaks_web.py
   ```
   This opens the desktop app, which uses a web-based interface.
3. **Login**: Enter your KovaaKs credentials.

## Controls

- **▶ Play**: Click the play icon or double-click a row to launch in KovaaKs.
- **🔁 Autoplay**: Enable to auto-advance through your list.
- **⟳ Refresh**: Sync latest scores and scenarios.
- **Right-click a column header**: Hide/show columns.
- **Right-click a row**: Play, copy the scenario name, or hide/unhide the scenario.

## Tests

```bash
python -m pip install -r requirements.txt pytest
python -m pytest -q
```

Tests use temporary caches, settings, logs, and stats directories. Install Node.js
to include the JavaScript rendering tests; those tests are skipped when Node.js
is unavailable. GitHub Actions runs the suite on Python 3.10 and 3.14 for pushes
and pull requests.

## Credits

- **evxl.app** - For making an actually functional website using the mess that is the KovaaKs API: [https://evxl.app/](https://evxl.app/)

---
*Open an issue if you hit bugs or have ideas. I might be slow to respond, depends on how I feel, my Antigravity quota and if I'm actively playing.*

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Naxeron/kovaaks-tracker)
