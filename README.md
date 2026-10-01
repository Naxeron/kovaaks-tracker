# ⌖ KovaaKs Scenario Tracker

A vibe-coded tracker for KovaaKs. Built for rank farming, sniping friends, and optimizing your practice.

![KovaaKs Scenario Tracker Screenshot](docs/assets/screenshot_v2.png)

## Features

- **Rank Tracking** — Live updates via KovaaKs API with accurate stats (unlike the official website).
- **Friend Sniping** — Compare scores side-by-side.
- **Potential Score** — Smart practice algorithm recommending scenarios based on skill gap, fatigue, and trends.
- **Autoplay** — Automatically launches the next scenario after you finish a run.

## Download and run

Download **kovaaks-tracker.zip** from an application release on the
[Releases page](https://github.com/Naxeron/kovaaks-tracker/releases) and extract
it. This download excludes tests, development tools, and generated datasets.
Python 3.10 or newer is required.

From the extracted folder, preferably in an activated virtual environment:

```bash
python -m pip install -r requirements/runtime.txt
python kovaaks_web.py
```

See the [installation guide](docs/INSTALL.md) for virtual environments, Linux
GUI dependencies, credential storage, and upgrading an existing installation.
The app keeps settings, logs, and cached scores together in `data/`.

GitHub's **Source code** downloads and Git clones still include the development
project. The `scenario-data` prerelease contains datasets, not the application.
If no application ZIP has been published yet, use the
[local build or manual workflow instructions](docs/DEVELOPMENT.md#build-an-application-download).

## Controls

- **▶ Play**: Click the play icon or double-click a row to launch in KovaaKs.
- **🔁 Autoplay**: Enable to auto-advance through your list.
- **⟳ Refresh**: Sync latest scores and scenarios.
- **Right-click a column header**: Hide/show columns.
- **Right-click a row**: Play, copy the scenario name, or hide/unhide the scenario.

## Development

The launcher is the only Python file in the root. Application code and web assets
live in `kovaaks/`; docs, dependency lists, scripts, and tests have their own folders.
README, license, and test configuration stay at the root for discoverability.

See [development and releases](docs/DEVELOPMENT.md) for source setup, the project
layout, testing, building a minimal ZIP, and maintaining scenario data.

## Credits

- **evxl.app** - For making an actually functional website using the mess that is the KovaaKs API: [https://evxl.app/](https://evxl.app/)

---
*Open an issue if you hit bugs or have ideas. I might be slow to respond, depends on how I feel, my Antigravity quota and if I'm actively playing.*

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Naxeron/kovaaks-tracker)
