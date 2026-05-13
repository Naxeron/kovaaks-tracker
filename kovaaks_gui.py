#!/usr/bin/env python3
"""
KovaaKs Scenario Tracker — Dark-themed GUI
Two tabs: Played (user + friends) and Unplayed.
Only considers scenarios with >1000 leaderboard entries.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import json
import os
import sys
import logging
import threading
import time
import base64
import datetime
import math
import urllib.parse
import webbrowser
import concurrent.futures

import requests

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logger = logging.getLogger("kovaaks")
logger.setLevel(logging.DEBUG)

_console_handler = logging.StreamHandler(sys.stderr)
_console_handler.setLevel(logging.DEBUG)
_console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_console_handler)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH  = os.path.join(SCRIPT_DIR, "config.json")
SCORES_CACHE = os.path.join(SCRIPT_DIR, "scores_cache.json")
LOG_FILE     = os.path.join(SCRIPT_DIR, "kovaaks.log")
MIN_ENTRIES  = 1000

def get_default_stats_dir():
    if sys.platform == "win32":
        return r"C:\Program Files (x86)\Steam\steamapps\common\FPSAimTrainer\FPSAimTrainer\stats"
    else:
        return os.path.expanduser("~/.local/share/Steam/steamapps/common/FPSAimTrainer/FPSAimTrainer/stats/")

# Session-based log trimming: keep at most 2 previous launches
_LAUNCH_MARKER = "=" * 60 + " LAUNCH " + "=" * 60

def _trim_log_file(path, keep=2):
    """Trim the log file to only keep the last *keep* launch sessions."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return
    marker = _LAUNCH_MARKER
    parts = content.split(marker)
    # parts[0] is text before the first marker (possibly empty),
    # parts[1..] each start right after a marker.
    if len(parts) <= keep + 1:
        return  # nothing to trim
    # Keep only the last `keep` sections (plus the text after their markers)
    trimmed = marker.join(parts[-(keep + 1):])
    with open(path, "w", encoding="utf-8") as f:
        f.write(trimmed)

_trim_log_file(LOG_FILE, keep=2)

_file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(_file_handler)

# Write the launch marker so future trims know where this session starts
logger.info(_LAUNCH_MARKER)

# ---------------------------------------------------------------------------
# KovaaKs API helpers
# ---------------------------------------------------------------------------

def _api_request_with_retry(method, url, timeout=30, max_retries=999, session=None, **kwargs):
    """Make an HTTP request with retry on timeouts/5xx and exponential backoff."""
    req_func = getattr(session, method.lower()) if session else getattr(requests, method.lower())
    for attempt in range(max_retries + 1):
        try:
            resp = req_func(url, timeout=timeout, **kwargs)
            if resp.status_code < 500 or attempt == max_retries:
                resp.raise_for_status()
                if attempt > 0:
                    logger.info("Recovered %s %s after %d retries", method.upper(), url, attempt)
                return resp
        except (requests.exceptions.RequestException, requests.exceptions.Timeout) as e:
            if hasattr(e, "response") and e.response is not None:
                if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                    raise
            if attempt == max_retries:
                raise
            logger.warning("Connection error %s, retrying %d/%d...", e, attempt + 1, max_retries)
        
        wait = min(60, 2 ** (attempt + 1))
        logger.warning("Server error/timeout, retrying in %ds…", wait)
        time.sleep(wait)
    return None


def _get_accurate_entry_count(leaderboard_id, session=None):
    """Fetch the accurate 'total' entries from the global leaderboard endpoint."""
    url = "https://kovaaks.com/webapp-backend/leaderboard/scores/global"
    params = {"leaderboardId": leaderboard_id, "page": 0, "max": 1}
    try:
        # Use retry logic for individual leaderboard requests too
        resp = _api_request_with_retry("get", url, params=params, timeout=15, max_retries=10, session=session)
        if resp:
            data = resp.json()
            return int(data.get("total", 0))
    except Exception as e:
        logger.debug("Failed to fetch accurate count for lid=%s: %s", leaderboard_id, e)
    return None


def _get_local_stats(stats_dir):
    """Extract local scenario stats (counts, recency, trends) from the Steam stats directory."""
    stats = {}
    if not os.path.exists(stats_dir):
        logger.warning("Stats directory not found: %s", stats_dir)
        return stats

    try:
        now_dt = datetime.datetime.now()
        filenames = os.listdir(stats_dir)
        # Sort filenames by date (descending) so we process most recent first
        # Pattern: ... - 2026.01.11-15.13.08 Stats.csv
        # We can just sort the strings since they are in YYYY.MM.DD format at the end
        filenames.sort(reverse=True)

        for fname in filenames:
            if fname.endswith(" Stats.csv"):
                # Pattern: [Scenario Name] - [Mode] - [Date] Stats.csv
                base = fname[:-10]
                parts = base.rsplit(" - ", 2)
                if len(parts) >= 3:
                    sname = parts[0]
                    date_str = parts[2]
                    try:
                        dt = datetime.datetime.strptime(date_str, "%Y.%m.%d-%H.%M.%S")
                    except ValueError:
                        continue
                    
                    if sname not in stats:
                        stats[sname] = {"count": 0, "last_played": dt, "recent_scores": [], "runs_today": 0}
                    
                    stats[sname]["count"] += 1
                    
                    if (now_dt - dt).total_seconds() < 86400:
                        stats[sname]["runs_today"] += 1
                    if dt > stats[sname]["last_played"]:
                        stats[sname]["last_played"] = dt
                    
                    # Store most recent scores for trend (already sorted by filenames.sort)
                    if len(stats[sname]["recent_scores"]) < 10:
                        fpath = os.path.join(stats_dir, fname)
                        try:
                            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                                for line in f:
                                    if line.startswith("Score:,"):
                                        score_val = float(line.split(",")[1])
                                        stats[sname]["recent_scores"].append((dt, score_val))
                                        break
                        except Exception:
                            pass

        # Calculate trends
        for sname, data in stats.items():
            # filenames were reversed, so scores are newest first. Reverse back for trend.
            scores = sorted(data["recent_scores"], key=lambda x: x[0])
            if len(scores) >= 2:
                changes = [scores[i][1] - scores[i-1][1] for i in range(1, len(scores))]
                avg_change = sum(changes) / len(changes)
                max_score = max(s[1] for s in scores)
                
                runs_since_pb = 0
                for i in range(len(scores)-1, -1, -1):
                    if scores[i][1] == max_score:
                        runs_since_pb = len(scores) - 1 - i
                        break
                if runs_since_pb == len(scores) - 1:
                    runs_since_pb = 999
                data["runs_since_recent_pb"] = runs_since_pb

                if max_score > 1.0: # avoid division by zero or tiny scores
                    # Trend factor: 1.0 is neutral. 
                    # If improving by 1% of max score per run, factor is 1.05
                    data["trend"] = max(0.5, min(2.0, 1.0 + (avg_change / max_score) * 5.0))
                else:
                    data["trend"] = 1.0
            else:
                data["trend"] = 1.0
                data["runs_since_recent_pb"] = 0
            del data["recent_scores"]

    except Exception as e:
        logger.error("Error reading local stats: %s", e)

    return stats


def fetch_all_scenarios(min_entries=0, session=None):
    """Fetch scenarios from the KovaaKs API (paginated, sorted by popularity).
    Stops early when all items on a page fall below *min_entries*.
    """
    url = "https://kovaaks.com/webapp-backend/scenario/popular"
    all_data = []
    page = 0
    while True:
        logger.debug("Fetching all scenarios page %d", page)
        params = {"page": page, "max": 100}
        resp = _api_request_with_retry("get", url, params=params, session=session)
        data = resp.json()
        items = data.get("data", [])
        if not items:
            break

        # Fetch accurate entry counts in parallel for the current page
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            future_to_item = {
                executor.submit(_get_accurate_entry_count, it.get("leaderboardId"), session): it
                for it in items
            }
            for future in concurrent.futures.as_completed(future_to_item):
                item = future_to_item[future]
                accurate_count = future.result()
                if accurate_count is not None:
                    # Update both the top-level counts and any nested scenario counts
                    if "counts" not in item:
                        item["counts"] = {}
                    item["counts"]["entries"] = accurate_count
                    if "scenario" in item and "counts" in item["scenario"]:
                        item["scenario"]["counts"]["entries"] = accurate_count

        all_data.extend(items)

        # Early stop: API returns by descending popularity
        if min_entries > 0:
            max_on_page = max(
                (int(it.get("counts", {}).get("entries", 0)) for it in items),
                default=0,
            )
            if max_on_page < min_entries:
                logger.info("Stopping at page %d — max entries %d < %d",
                            page, max_on_page, min_entries)
                break

        total = data.get("total", 0)
        page += 1
        if len(all_data) >= total:
            break
        time.sleep(0.1)

    logger.info("Fetched %d total scenarios with accurate counts", len(all_data))
    return all_data


def load_scenarios_from_cache(cache):
    """Extract cached scenario list from the unified cache dict."""
    scenarios = cache.get("scenarios", [])
    if scenarios:
        logger.debug("Loaded %d scenarios from JSON cache", len(scenarios))
    return scenarios

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

# Steam launch URI for KovaaKs (App ID 824270)
STEAM_LAUNCH_URI = "steam://run/824270/?action=jump-to-scenario;name={}"

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------
COLUMNS = [
    ("▶", 32),
    ("Scenario", 240),
    ("Entry Count", 80),
    ("New Entries / Day", 120),
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
    ("Utility", 70),
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
                     "Rank Diff", "Pctile Diff", "Local Runs", "Utility"},
}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return {}

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Stdout redirector
# ---------------------------------------------------------------------------

class StdoutRedirector:
    """Redirect stdout/stderr writes to a callback while keeping the original."""

    def __init__(self, callback, original):
        self._callback = callback
        self._original = original

    def write(self, text):
        if self._original:
            self._original.write(text)
        if text and text.strip():
            self._callback(text.strip())
        return len(text) if text else 0

    def flush(self):
        if self._original:
            self._original.flush()


# ---------------------------------------------------------------------------
# KovaaKs API helpers
# ---------------------------------------------------------------------------

_KOVAAKS_HEADERS = {
    "Origin": "https://evxl.app",
    "Referer": "https://evxl.app/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


def kovaaks_login(username, password):
    """Login to KovaaKs webapp, return JWT token string."""
    logger.debug("Logging in to KovaaKs as '%s'", username)
    url = "https://kovaaks.com/auth/webapp/login"
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {**_KOVAAKS_HEADERS, "Authorization": f"Basic {credentials}"}
    resp = _api_request_with_retry("post", url, headers=headers, data="", timeout=15)

    data = resp.json()
    auth = data.get("auth", {})
    logger.debug("Login auth keys: %s", list(auth.keys()) if isinstance(auth, dict) else type(auth))

    if isinstance(auth, dict):
        for key in ("jwt", "token", "access_token", "firebaseJWT"):
            token = auth.get(key)
            if token and isinstance(token, str) and token.startswith("eyJ"):
                logger.info("Login successful (token from auth.%s, len=%d)", key, len(token))
                return token

    raise ValueError(f"Could not find JWT in login response. Keys: {list(data.keys())}")


def kovaaks_get_friends_scores(token, leaderboard_id, session=None):
    """Fetch friends' scores for a given leaderboard ID."""
    url = "https://kovaaks.com/webapp-backend/leaderboard/scores/friends"
    headers = {**_KOVAAKS_HEADERS, "Authorization": f"Bearer {token}"}
    resp = _api_request_with_retry("get", url, params={
        "leaderboardId": leaderboard_id,
        "page": 0,
        "max": 50,
    }, headers=headers, timeout=30, session=session)
    return resp.json().get("data", [])





def natural_sort_key(val):
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
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog(tk.Toplevel):
    """Modal dialog for KovaaKs username and password."""

    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result = None

        pad = {"padx": 12, "pady": 6}
        row = 0

        fields = [
            ("KovaaKs Username", "username", cfg.get("username", "")),
            ("KovaaKs Password", "password", cfg.get("password", "")),
            ("Stats Folder", "stats_dir", cfg.get("stats_dir", get_default_stats_dir())),
        ]
        self._entries = {}

        for label_text, key, default in fields:
            ttk.Label(self, text=label_text, style="Dark.TLabel").grid(
                row=row, column=0, sticky="w", **pad)
            var = tk.StringVar(value=default)
            show_char = "*" if key == "password" else ""
            entry = tk.Entry(self, textvariable=var, width=42,
                             bg=ENTRY_BG, fg=TEXT, insertbackground=TEXT,
                             font=("Segoe UI", 11), relief="flat", bd=4,
                             show=show_char)
            entry.grid(row=row, column=1, **pad)
            self._entries[key] = var
            row += 1

        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=12)

        save_btn = tk.Button(btn_frame, text="  Save  ", command=self._on_save,
                             bg=ACCENT, fg="#fff", activebackground=ACCENT_HOVER,
                             activeforeground="#fff", font=("Segoe UI", 10, "bold"),
                             relief="flat", bd=0, padx=14, pady=6, cursor="hand2")
        save_btn.pack(side="left", padx=8)

        cancel_btn = tk.Button(btn_frame, text="  Cancel  ", command=self.destroy,
                               bg=BG_LIGHTER, fg=TEXT, activebackground=BORDER,
                               activeforeground=TEXT, font=("Segoe UI", 10),
                               relief="flat", bd=0, padx=14, pady=6, cursor="hand2")
        cancel_btn.pack(side="left", padx=8)

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _on_save(self):
        self.result = {k: v.get().strip() for k, v in self._entries.items()}
        self.destroy()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class KovaaksApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("KovaaKs Scenario Tracker")
        self.geometry("1280x780")
        self.minsize(900, 500)
        self.configure(bg=BG)

        self._cfg = load_config()

        self._all_data: list[dict] = []
        self._tree: ttk.Treeview | None = None
        self._filter_var = tk.StringVar()
        self._count_var = tk.StringVar(value="")
        self._sort_state: tuple[str, bool] | None = None
        self._filters: dict[str, tk.BooleanVar] = {}

        # Column visibility — ▶ and Scenario are always shown
        always_visible = {"▶", "Scenario"}
        saved = self._cfg.get("visible_columns", None)
        self._visible_cols: dict[str, tk.BooleanVar] = {}
        for col_name, _ in COLUMNS:
            if col_name in always_visible:
                continue
            default = saved is None or col_name in saved
            
            # Force recently added columns to be visible even if old config is loaded
            if col_name in {"New Entries / Day", "Trend Mult"} and saved and col_name not in saved:
                default = True
                
            self._visible_cols[col_name] = tk.BooleanVar(value=default)

        self._running = False
        self._all_buttons: list[tk.Button] = []
        self._jwt_token: str | None = None
        self._scenario_info: dict[str, dict] = {}
        self._user_by_lid: dict[str, dict] = {}
        self._friends_by_lid: dict[str, list] = {}
        self._scores_cache: dict[str, dict] = {}
        self._known_stat_files: set[str] = set()
        self._poll_stats_id: str | None = None
        self._autofit_pending: str | None = None  # after() ID for debounced auto-fit
        self._last_window_width: int = 0

        # Autoplay state
        self._autoplay_var = tk.BooleanVar(value=False)
        self._autoplay_current_scenario: str | None = None
        self._autoplay_btn: tk.Button | None = None

        self._apply_styles()
        self._build_ui()
        self._setup_log_redirect()

        # Load cache and populate view immediately
        self._load_cache_and_populate()

        # Show settings on first run
        if not self._cfg.get("username") or not self._cfg.get("password"):
            self.after(300, self._on_settings)

        self._start_stats_polling()
        logger.info("Application started")

        # Auto-fit columns on window resize
        self.bind("<Configure>", self._on_window_resize)
        
        # Schedule auto refresh
        self.after(3600000, self._auto_refresh_step)

    def _get_stats_dir(self):
        path = self._cfg.get("stats_dir", get_default_stats_dir())
        return os.path.expanduser(path) if path else ""

    def _auto_refresh_step(self):
        if not self._running:
            username = self._cfg.get("username", "").strip()
            password = self._cfg.get("password", "").strip()
            if username and password:
                self._update_status("Auto-refreshing...")
                self._set_running(True, "Auto-refreshing…")
                threading.Thread(target=self._do_fetch_all, args=(username, password), daemon=True).start()
        self.after(3600000, self._auto_refresh_step)

    # -------------------------------------------------------------------
    # Styles
    # -------------------------------------------------------------------
    def _apply_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("Dark.Treeview",
                        background=TREE_BG, foreground=TREE_FG,
                        fieldbackground=TREE_BG, rowheight=28,
                        font=("Segoe UI", 10), borderwidth=0)
        style.configure("Dark.Treeview.Heading",
                        background=HEADER_BG, foreground=TEXT,
                        font=("Segoe UI", 10, "bold"), borderwidth=1,
                        relief="flat")
        style.map("Dark.Treeview",
                  background=[("selected", TREE_SEL_BG)],
                  foreground=[("selected", TREE_SEL_FG)])
        style.map("Dark.Treeview.Heading",
                  background=[("active", ACCENT)])

        style.configure("Dark.TFrame", background=BG)
        style.configure("Dark.TLabel", background=BG, foreground=TEXT,
                        font=("Segoe UI", 10))
        style.configure("Status.TLabel", background=BG_DARKER, foreground=TEXT_DIM,
                        font=("Segoe UI", 9), padding=[8, 4])
        style.configure("Title.TLabel", background=BG, foreground=ACCENT,
                        font=("Segoe UI", 18, "bold"))

        style.configure("Dark.Vertical.TScrollbar",
                        background=BG_LIGHTER, troughcolor=BG_DARKER,
                        arrowcolor=TEXT)

    # -------------------------------------------------------------------
    # UI construction
    # -------------------------------------------------------------------
    def _build_ui(self):
        # — Top bar —
        top_frame = tk.Frame(self, bg=BG, pady=12, padx=16)
        top_frame.pack(fill="x")

        title = ttk.Label(top_frame, text="⌖  KovaaKs Scenario Tracker",
                          style="Title.TLabel")
        title.pack(side="left")

        # Controls (right side)
        ctrl = tk.Frame(top_frame, bg=BG)
        ctrl.pack(side="right")

        self._btn_autofit = self._make_button(ctrl, "⇔ Fit Columns", self._auto_resize_columns)
        self._btn_autofit.pack(side="left", padx=4)

        self._btn_refresh = self._make_button(ctrl, "⟳ Refresh", self._on_fetch_all)
        self._btn_refresh.pack(side="left", padx=4)

        self._btn_settings = self._make_button(ctrl, "⚙", self._on_settings)
        self._btn_settings.pack(side="left", padx=(8, 0))

        # — Separator —
        sep = tk.Frame(self, bg=BORDER, height=1)
        sep.pack(fill="x")

        # — Main content area —
        content = tk.Frame(self, bg=BG)
        content.pack(fill="both", expand=True)

        # Filter bar
        filter_frame = tk.Frame(content, bg=BG, pady=8, padx=12)
        filter_frame.pack(fill="x")

        ttk.Label(filter_frame, text="🔍 Filter:", style="Dark.TLabel").pack(side="left")
        filter_entry = tk.Entry(
            filter_frame, textvariable=self._filter_var, width=30,
            bg=ENTRY_BG, fg=TEXT, insertbackground=TEXT,
            font=("Segoe UI", 10), relief="flat", bd=4)
        filter_entry.pack(side="left", padx=(8, 0))
        self._filter_var.trace_add("write", lambda *_a: self._apply_filter())

        count_label = ttk.Label(filter_frame, textvariable=self._count_var,
                                style="Dark.TLabel")
        count_label.pack(side="right", padx=8)

        # Toggle filter buttons
        toggle_frame = tk.Frame(content, bg=BG, padx=12)
        toggle_frame.pack(fill="x")

        for filter_key, label_text in [
            ("losing", "👎 Losing"),
            ("friends_only", "👥 Friends Only"),
            ("me_only", "🙋 Me Only"),
            ("unplayed", "❌ Unplayed"),
        ]:
            var = tk.BooleanVar(value=False)
            self._filters[filter_key] = var
            self._make_toggle_button(toggle_frame, label_text, var)

        # Spacer
        tk.Frame(toggle_frame, bg=BG, width=16).pack(side="left")

        # Autoplay toggle button
        self._autoplay_btn = tk.Button(
            toggle_frame, text="🔁 Autoplay",
            bg=BG_LIGHTER, fg=TEXT_DIM, activebackground=GREEN,
            activeforeground="#fff", font=("Segoe UI", 9, "bold"),
            relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
            command=self._toggle_autoplay)
        self._autoplay_btn.pack(side="left", padx=(0, 6), pady=(0, 6))

        # Treeview + scrollbars (grid layout so vsb is never clipped)
        tree_frame = tk.Frame(content, bg=BG)
        tree_frame.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        columns = [c[0] for c in COLUMNS]
        self._tree = ttk.Treeview(tree_frame, columns=columns,
                                  show="headings", style="Dark.Treeview",
                                  selectmode="browse")
        for col_name, col_width in COLUMNS:
            if col_name == "▶":
                self._tree.heading(col_name, text="▶")
                self._tree.column(col_name, width=col_width,
                                  minwidth=col_width, stretch=False,
                                  anchor="center")
            else:
                self._tree.heading(col_name, text=col_name,
                                   command=lambda c=col_name: self._on_sort(c))
                self._tree.column(col_name, width=col_width, minwidth=50)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self._tree.yview,
                            style="Dark.Vertical.TScrollbar")
        self._hsb = ttk.Scrollbar(tree_frame, orient="horizontal",
                            command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set,
                             xscrollcommand=self._on_hsb_set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self._hsb.grid(row=1, column=0, sticky="ew")
        self._hsb.grid_remove()

        self._tree.tag_configure("odd", background=ALT_ROW)
        self._tree.tag_configure("even", background=TREE_BG)

        # Double-click → copy scenario name
        self._tree.bind("<Double-1>", self._copy_scenario_name)

        # Single-click on ▶ column → play scenario
        self._tree.bind("<ButtonRelease-1>", self._on_tree_click)

        # Enter key → play selected scenario
        self._tree.bind("<Return>", lambda e: self._play_scenario())

        # Right-click → column visibility menu
        self._tree.bind("<Button-3>", self._show_column_menu)

        # Apply saved column visibility
        self._refresh_columns(save=False)

        # — Log panel (collapsible + resizable) —
        self._log_frame = tk.Frame(self, bg=BG_DARKER)
        self._log_height = 160  # default height in pixels
        self._log_min_height = 60
        self._log_max_height = 500
        self._log_drag_start_y = None
        self._log_drag_start_h = None

        # Resize handle (drag bar at top of log frame)
        self._log_resize_handle = tk.Frame(self._log_frame, bg=BORDER, height=4, cursor="sb_v_double_arrow")
        self._log_resize_handle.pack(fill="x")
        self._log_resize_handle.bind("<ButtonPress-1>", self._log_resize_start)
        self._log_resize_handle.bind("<B1-Motion>", self._log_resize_drag)
        self._log_resize_handle.bind("<Enter>", lambda e: self._log_resize_handle.configure(bg=ACCENT))
        self._log_resize_handle.bind("<Leave>", lambda e: self._log_resize_handle.configure(bg=BORDER))

        self._log_header = tk.Frame(self._log_frame, bg=BG_DARKER)
        self._log_header.pack(fill="x")
        self._log_visible = False

        self._log_toggle_btn = tk.Button(
            self._log_header, text="▼ Log", command=self._toggle_log,
            bg=BG_DARKER, fg=TEXT_DIM, activebackground=BG_LIGHTER,
            activeforeground=TEXT, font=("Segoe UI", 9, "bold"),
            relief="flat", bd=0, padx=8, pady=2, cursor="hand2",
            anchor="w")
        self._log_toggle_btn.pack(side="left")

        clear_btn = tk.Button(
            self._log_header, text="Clear", command=self._clear_log,
            bg=BG_DARKER, fg=TEXT_DIM, activebackground=BG_LIGHTER,
            activeforeground=TEXT, font=("Segoe UI", 8),
            relief="flat", bd=0, padx=6, pady=2, cursor="hand2")
        clear_btn.pack(side="right", padx=4)

        # Log text inside a container so we can control height
        self._log_text_container = tk.Frame(self._log_frame, bg=LOG_BG)

        self._log_text = tk.Text(
            self._log_text_container, bg=LOG_BG, fg=LOG_FG,
            font=("Consolas", 9), relief="flat", bd=4,
            insertbackground=LOG_FG, wrap="word", state="disabled")

        log_scrollbar = ttk.Scrollbar(
            self._log_text_container, orient="vertical", command=self._log_text.yview,
            style="Dark.Vertical.TScrollbar")
        self._log_text.configure(yscrollcommand=log_scrollbar.set)

        log_scrollbar.pack(side="right", fill="y")
        self._log_text.pack(side="left", fill="both", expand=True)

        # Start collapsed — text container hidden
        # (don't pack _log_text_container yet)

        self._log_frame.pack(fill="x", side="bottom")

        # — Log toggle starts collapsed —
        self._log_toggle_btn.configure(text="▶ Log")
        self._log_resize_handle.pack_forget()  # hide resize handle when collapsed

        # — Status bar —
        self._status_var = tk.StringVar(value="Ready")
        status = ttk.Label(self, textvariable=self._status_var, style="Status.TLabel")
        status.pack(fill="x", side="bottom")

    def _make_button(self, parent, text, command):
        btn = tk.Button(
            parent, text=text, command=command,
            bg=ACCENT, fg="#fff", activebackground=ACCENT_HOVER,
            activeforeground="#fff", font=("Segoe UI", 10, "bold"),
            relief="flat", bd=0, padx=14, pady=6, cursor="hand2")
        btn.bind("<Enter>", lambda e, b=btn: b.configure(bg=ACCENT_HOVER))
        btn.bind("<Leave>", lambda e, b=btn: b.configure(bg=ACCENT))
        self._all_buttons.append(btn)
        return btn

    def _make_toggle_button(self, parent, text, var):
        """Create a toggle button that highlights when active."""
        btn = tk.Button(
            parent, text=text,
            bg=BG_LIGHTER, fg=TEXT_DIM, activebackground=ACCENT,
            activeforeground="#fff", font=("Segoe UI", 9, "bold"),
            relief="flat", bd=0, padx=10, pady=4, cursor="hand2")

        def toggle():
            var.set(not var.get())
            if var.get():
                btn.configure(bg=ACCENT, fg="#fff")
            else:
                btn.configure(bg=BG_LIGHTER, fg=TEXT_DIM)
            self._apply_filter()

        btn.configure(command=toggle)
        btn.pack(side="left", padx=(0, 6), pady=(0, 6))
        return btn

    def _on_hsb_set(self, first, last):
        """Show horizontal scrollbar only when content overflows."""
        if float(first) <= 0.0 and float(last) >= 1.0:
            self._hsb.grid_remove()
        else:
            self._hsb.grid()
        self._hsb.set(first, last)

    def _schedule_autofit(self):
        """Debounced auto-fit: schedules _auto_resize_columns after a short delay.
        Multiple rapid calls (e.g. filter + populate + column refresh) collapse into one."""
        if self._autofit_pending:
            self.after_cancel(self._autofit_pending)
        self._autofit_pending = self.after(50, self._do_autofit)

    def _do_autofit(self):
        """Execute the debounced auto-fit."""
        self._autofit_pending = None
        self._auto_resize_columns()

    def _on_window_resize(self, event):
        """Handle window resize — trigger auto-fit when width changes."""
        # Only react to the top-level window's own Configure events
        if event.widget is not self:
            return
        if event.width != self._last_window_width:
            self._last_window_width = event.width
            self._schedule_autofit()

    def _auto_resize_columns(self):
        """Resize each column to the smallest width that fits its content."""
        import tkinter.font as tkfont
        tree = self._tree
        font = tkfont.Font(family="Segoe UI", size=10)
        heading_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        padding = 16
        children = tree.get_children()
        if not children:
            return

        for col_name, default_width in COLUMNS:
            if col_name == "▶":
                continue
            longest = ""
            for iid in children:
                val = str(tree.set(iid, col_name))
                if len(val) > len(longest):
                    longest = val
            header_text = tree.heading(col_name, "text")
            max_w = heading_font.measure(header_text) + padding + 12
            cell_w = font.measure(longest) + padding
            if cell_w > max_w:
                max_w = cell_w
            tree.column(col_name, width=max_w, minwidth=50)

    def _copy_scenario_name(self, event=None):
        """Copy the scenario name of the clicked row to clipboard."""
        row_id = self._tree.identify_row(event.y) if event else ""
        if not row_id:
            return
        values = self._tree.item(row_id, "values")
        if not values:
            return
        name = values[1]  # Scenario is the second column (after ▶)
        try:
            import subprocess
            proc = subprocess.Popen(
                ["xclip", "-selection", "clipboard"],
                stdin=subprocess.PIPE)
            proc.communicate(name.encode("utf-8"))
        except FileNotFoundError:
            self.clipboard_clear()
            self.clipboard_append(name)
            self.update()
        self._update_status(f"Copied: {name}")

    def _on_tree_click(self, event):
        """Handle single-click on the treeview — play if ▶ column clicked."""
        region = self._tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col_id = self._tree.identify_column(event.x)
        if col_id == "#1":  # ▶ is the first column
            self._play_scenario(event)

    def _play_scenario(self, event=None):
        """Launch the selected scenario in KovaaKs via Steam."""
        if event:
            row_id = self._tree.identify_row(event.y)
        else:
            sel = self._tree.selection()
            row_id = sel[0] if sel else ""
        if not row_id:
            self._update_status("Select a scenario first")
            return
        values = self._tree.item(row_id, "values")
        if not values:
            return
        name = values[1]  # Scenario is the second column (after ▶)
        uri = STEAM_LAUNCH_URI.format(urllib.parse.quote(name, safe=""))
        webbrowser.open(uri)

        # If autoplay is active, update tracking to this scenario
        if self._autoplay_var.get():
            self._autoplay_current_scenario = name
            self._update_status(f"Autoplay: launching '{name}' — waiting for score…")
        else:
            self._update_status(f"Launching: {name}")

    # -------------------------------------------------------------------
    # Cache loading
    # -------------------------------------------------------------------
    def _load_cache_and_populate(self):
        """Load the unified JSON cache and populate tabs with cached data."""
        self._scores_cache = {}
        if os.path.exists(SCORES_CACHE):
            try:
                with open(SCORES_CACHE, "r", encoding="utf-8") as f:
                    self._scores_cache = json.load(f)
                logger.info("Loaded cache from %s", SCORES_CACHE)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Could not load cache: %s", e)

        all_scenarios = load_scenarios_from_cache(self._scores_cache)
        if not all_scenarios:
            return

        # Filter to >=MIN_ENTRIES
        master = []
        for s in all_scenarios:
            entries = s.get("counts", {}).get("entries", 0)
            try:
                entries = int(entries)
            except (ValueError, TypeError):
                entries = 0
            if entries >= MIN_ENTRIES:
                master.append(s)

        if not master:
            return

        # Build lid -> scenario info map
        scenario_info = {}
        for s in master:
            lid = str(s.get("leaderboardId", ""))
            sd = s.get("scenario", s)
            authors = sd.get("authors", [])
            if isinstance(authors, list):
                authors = ", ".join(authors)
            scenario_info[lid] = {
                "name": s.get("scenarioName", ""),
                "entries": s.get("counts", {}).get("entries", ""),
                "description": sd.get("description", ""),
                "authors": str(authors),
            }

        # Extract scores
        scores_data = self._scores_cache.get("scores", {})
        if not scores_data and any(
            k not in ("scenarios", "scores") for k in self._scores_cache
        ):
            scores_data = {
                k: v for k, v in self._scores_cache.items()
                if k not in ("scenarios",)
            }

        user_by_lid = {}
        friends_by_lid = {}
        for lid, cached in scores_data.items():
            if lid in scenario_info:
                if "user" in cached:
                    user_by_lid[lid] = cached["user"]
                if "friends" in cached and cached["friends"]:
                    friends_by_lid[lid] = cached["friends"]

        self._scenario_info = scenario_info
        self._user_by_lid = user_by_lid
        self._friends_by_lid = friends_by_lid

        played, unplayed = self._rebuild_data()
        self._update_status(
            f"Loaded from cache — {played} played, {unplayed} unplayed"
        )

    # -------------------------------------------------------------------
    # Settings
    # -------------------------------------------------------------------
    def _on_settings(self):
        old_stats_dir = self._cfg.get("stats_dir")
        dlg = SettingsDialog(self, self._cfg)
        self.wait_window(dlg)
        if dlg.result:
            self._cfg.update(dlg.result)
            save_config(self._cfg)
            self._update_status("Settings saved.")

            new_stats_dir = self._cfg.get("stats_dir")
            if old_stats_dir != new_stats_dir:
                self._known_stat_files.clear()
                stats_dir = self._get_stats_dir()
                if os.path.exists(stats_dir):
                    try:
                        for f in os.listdir(stats_dir):
                            if f.endswith(" Stats.csv"):
                                self._known_stat_files.add(f)
                    except OSError:
                        pass
                self._rebuild_data()

    # -------------------------------------------------------------------
    # Data display
    # -------------------------------------------------------------------
    def _populate_tree(self, rows):
        yview = self._tree.yview()
        selected = self._tree.selection()
        selected_scenarios = set()
        for item in selected:
            vals = self._tree.item(item, "values")
            if vals and len(vals) > 1:
                selected_scenarios.add(vals[1])

        self._tree.delete(*self._tree.get_children())
        cols = [c[0] for c in COLUMNS]
        
        items_to_select = []
        for i, row in enumerate(rows):
            values = [row.get(c, "") for c in cols]
            values[0] = "▶"  # Play icon in first column
            tag = "odd" if i % 2 else "even"
            item = self._tree.insert("", "end", values=values, tags=(tag,))
            
            if len(values) > 1 and values[1] in selected_scenarios:
                items_to_select.append(item)
                
        self._count_var.set(f"{len(rows)} rows")
        self._schedule_autofit()
        
        if items_to_select:
            self._tree.selection_set(items_to_select)
        if yview:
            self.after(0, lambda: self._tree.yview_moveto(yview[0]))

    def _apply_filter(self):
        query = self._filter_var.get().lower().strip()
        all_rows = list(self._all_data)

        # Apply toggle filters
        if self._filters:
            losing = self._filters["losing"].get()
            friends_only = self._filters["friends_only"].get()
            me_only = self._filters["me_only"].get()
            unplayed = self._filters["unplayed"].get()

            if losing or friends_only or me_only or unplayed:
                filtered = []
                for r in all_rows:
                    has_rank = r.get("My Rank", "") != ""
                    has_friend = r.get("Best Friend", "") != ""
                    rank_diff = r.get("Rank Diff", "")

                    if losing and has_rank and has_friend and rank_diff:
                        try:
                            if int(rank_diff) > 0:
                                filtered.append(r)
                        except (ValueError, TypeError):
                            pass
                        continue
                    if friends_only and has_friend and not has_rank:
                        filtered.append(r)
                        continue
                    if me_only and has_rank and not has_friend:
                        filtered.append(r)
                        continue
                    if unplayed and not has_rank and not has_friend:
                        filtered.append(r)
                        continue
                all_rows = filtered

        if query:
            all_rows = [r for r in all_rows if any(
                query in str(v).lower() for v in r.values())]

        self._populate_tree(all_rows)
        self._refresh_columns(save=False)

    def _on_sort(self, column):
        prev = self._sort_state
        reverse = False
        if prev and prev[0] == column:
            reverse = not prev[1]
        self._sort_state = (column, reverse)
        self._apply_current_sort()
        self._apply_filter()

    def _apply_current_sort(self):
        if not self._sort_state:
            return
        column, reverse = self._sort_state

        # Separate items with and without values to keep empty items at the bottom
        has_val = []
        no_val = []
        for r in self._all_data:
            if str(r.get(column, "")).strip():
                has_val.append(r)
            else:
                no_val.append(r)

        has_val.sort(
            key=lambda r: natural_sort_key(r.get(column, "")),
            reverse=reverse)

        self._all_data = has_val + no_val

        cols = [c[0] for c in COLUMNS]
        for c in cols:
            arrow = ""
            if c == column:
                arrow = " ▼" if reverse else " ▲"
            self._tree.heading(c, text=c + arrow)

    # -------------------------------------------------------------------
    # Fetch All — unified fetch operation
    # -------------------------------------------------------------------
    def _set_running(self, running, status="Ready"):
        self._running = running
        state = "disabled" if running else "normal"
        for btn in self._all_buttons:
            btn.configure(state=state)
        self._status_var.set(status)

    def _on_fetch_all(self):
        username = self._cfg.get("username", "").strip()
        if not username:
            messagebox.showwarning("Username required",
                                   "Please configure your KovaaKs username in Settings.")
            self._on_settings()
            return
        password = self._cfg.get("password", "").strip()
        if not password:
            messagebox.showwarning("Settings required",
                                   "Please configure your KovaaKs password in Settings.")
            self._on_settings()
            return
        if self._running:
            return

        self._set_running(True, "Starting…")
        threading.Thread(target=self._do_fetch_all, args=(username, password),
                         daemon=True).start()

    def _do_fetch_all(self, username, password):
        try:
            # ── Step 1: Login ──
            self._update_status("Logging in to KovaaKs…")
            try:
                self._jwt_token = kovaaks_login(username, password)
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 401:
                    self._update_status("Error: Invalid KovaaKs username or password.")
                else:
                    self._update_status(f"Login error: {e}")
                return
            logger.debug("JWT token obtained (length=%d)", len(self._jwt_token))

            # ── Step 2: Fetch all scenarios (>1000 entries) ──
            self._update_status("Fetching all scenarios (accurate counts)…")

            # Reuse cache loaded at startup
            scores_cache = self._scores_cache

            session = requests.Session()
            all_scenarios = fetch_all_scenarios(min_entries=MIN_ENTRIES, session=session)
            logger.info("API returned %d total scenarios", len(all_scenarios))

            # Save scenarios to cache
            scores_cache["scenarios"] = all_scenarios
            try:
                with open(SCORES_CACHE, "w", encoding="utf-8") as f:
                    json.dump(scores_cache, f, separators=(",", ":"))
                logger.info("Saved scenarios to cache %s", SCORES_CACHE)
            except OSError as e:
                logger.warning("Could not save cache: %s", e)

            # Filter to >1000 entries
            master = []
            for s in all_scenarios:
                entries = s.get("counts", {}).get("entries", 0)
                try:
                    entries = int(entries)
                except (ValueError, TypeError):
                    entries = 0
                if entries >= MIN_ENTRIES:
                    master.append(s)

            logger.info("Filtered to %d scenarios with >=%d entries", len(master), MIN_ENTRIES)
            self._update_status(f"{len(master)} scenarios with ≥{MIN_ENTRIES} entries.")

            now_str = datetime.datetime.now().isoformat()
            history = scores_cache.get("entry_history", {})
            for s in master:
                lid = str(s.get("leaderboardId", ""))
                entries = s.get("counts", {}).get("entries", 0)
                try:
                    entries = int(entries)
                except (ValueError, TypeError):
                    continue
                if lid not in history:
                    history[lid] = {}
                history[lid][now_str] = entries
                
                # Prune to last 48 records max (e.g. 2 days of hourly data)
                if len(history[lid]) > 48:
                    oldest_key = min(history[lid].keys())
                    del history[lid][oldest_key]
                    
            scores_cache["entry_history"] = history

            # Build lid → scenario info map
            scenario_info = {}  # lid → {name, entries, description, authors}
            for s in master:
                lid = str(s.get("leaderboardId", ""))
                sd = s.get("scenario", s)
                authors = sd.get("authors", [])
                if isinstance(authors, list):
                    authors = ", ".join(authors)
                scenario_info[lid] = {
                    "name": s.get("scenarioName", ""),
                    "entries": s.get("counts", {}).get("entries", ""),
                    "description": sd.get("description", ""),
                    "authors": str(authors),
                }

            # ── Step 3+4: Fetch user + friends' scores together (concurrent) ──
            # The friends endpoint returns the logged-in user's own score too,
            # with the correct rank even for tied scores.

            # Clear scores so everything is re-fetched
            scores_data = {}

            # Pre-populate from cache
            user_by_lid = {}    # lid → {rank, score, date}
            friends_by_lid = {} # lid → list of {friend, rank, score}
            for lid, cached in scores_data.items():
                if lid in scenario_info:  # only keep lids that are in master
                    if "user" in cached:
                        user_by_lid[lid] = cached["user"]
                    if "friends" in cached and cached["friends"]:
                        friends_by_lid[lid] = cached["friends"]

            # Only fetch lids not already cached
            all_lids = list(scenario_info.keys())
            work_items = [lid for lid in all_lids if lid not in scores_data]
            total_all = len(all_lids)
            total_to_fetch = len(work_items)
            cached_count = total_all - total_to_fetch
            logger.info("%d cached, %d to fetch (of %d total)",
                        cached_count, total_to_fetch, total_all)

            self._scenario_info = scenario_info
            self._user_by_lid = user_by_lid
            self._friends_by_lid = friends_by_lid

            if cached_count > 0:
                self._update_status(
                    f"{cached_count} cached, fetching {total_to_fetch} remaining…"
                )
                # Show cached data immediately
                self._rebuild_data()

            if total_to_fetch == 0:
                played, unplayed = self._rebuild_data()
                self._update_status(
                    f"Done (all from cache) — {played} played, {unplayed} unplayed"
                )
                return

            lock = threading.Lock()
            errors = 0
            completed = 0
            session_expired = False
            start_time = time.time()
            last_refresh = [0]
            last_save = [0]
            eta_window = []

            def _save_cache():
                """Save unified cache to disk (call under lock)."""
                try:
                    unified = {
                        "scenarios": scores_cache.get("scenarios", []),
                        "scores": scores_data,
                        "entry_history": scores_cache.get("entry_history", {}),
                    }
                    with open(SCORES_CACHE, "w", encoding="utf-8") as f:
                        json.dump(unified, f, separators=(",", ":"))
                except OSError as e:
                    logger.debug("Cache save error: %s", e)

            def _fetch_one(lid, session):
                nonlocal errors, completed, session_expired
                if session_expired:
                    return

                try:
                    data = kovaaks_get_friends_scores(self._jwt_token, lid, session=session)
                except requests.exceptions.HTTPError as e:
                    if e.response is not None and e.response.status_code == 401:
                        session_expired = True
                        return
                    with lock:
                        errors += 1
                    logger.debug("Error for lid=%s: %s", lid, e)
                    return
                except Exception as ex:
                    with lock:
                        errors += 1
                    logger.debug("Error for lid=%s: %s", lid, ex)
                    return

                if data is None:
                    return

                friend_entries = []
                user_entry = None
                for entry in data:
                    name = entry.get("webappUsername") or entry.get("steamAccountName", "")
                    if name.lower() == username.lower():
                        epoch = entry.get("attributes", {}).get("epoch", "")
                        score_date = ""
                        if epoch:
                            try:
                                score_date = datetime.datetime.fromtimestamp(
                                    int(epoch) / 1000
                                ).strftime("%Y-%m-%d")
                            except (ValueError, TypeError, OSError):
                                pass
                        user_entry = {
                            "rank": entry.get("rank", ""),
                            "score": entry.get("score", ""),
                            "date": score_date,
                        }
                    else:
                        f_epoch = entry.get("attributes", {}).get("epoch", "")
                        f_date = ""
                        if f_epoch:
                            try:
                                f_date = datetime.datetime.fromtimestamp(
                                    int(f_epoch) / 1000
                                ).strftime("%Y-%m-%d")
                            except (ValueError, TypeError, OSError):
                                pass
                        friend_entries.append({
                            "friend": name,
                            "rank": entry.get("rank", ""),
                            "score": entry.get("score", ""),
                            "date": f_date,
                        })

                with lock:
                    cache_entry = {}
                    if user_entry:
                        user_by_lid[lid] = user_entry
                        cache_entry["user"] = user_entry
                    if friend_entries:
                        friends_by_lid[lid] = friend_entries
                        cache_entry["friends"] = friend_entries
                    scores_data[lid] = cache_entry

                    completed += 1
                    done = completed

                    # Save cache every 200 completions
                    if done - last_save[0] >= 200 or done == total_to_fetch:  # noqa: E501
                        last_save[0] = done
                        _save_cache()

                if done % 20 == 0 or done == total_to_fetch:
                    now = time.time()
                    eta_window.append((done, now))
                    if len(eta_window) > 10:
                        eta_window.pop(0)
                    if len(eta_window) >= 2:
                        wd, wt = eta_window[0]
                        rate = (done - wd) / (now - wt) if now > wt else 0
                    else:
                        rate = done / (now - start_time) if now > start_time else 0
                    remaining = (total_to_fetch - done) / rate if rate > 0 else 0
                    mins, secs = divmod(int(remaining), 60)
                    eta = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
                    self._update_status(
                        f"Fetching scores… {done}/{total_to_fetch} "
                        f"({cached_count} cached, {errors} errors) — ETA {eta}"
                    )
                # Live-refresh tabs every 100 completions
                if done - last_refresh[0] >= 100:
                    last_refresh[0] = done
                    self._rebuild_data()

            session = requests.Session()
            with concurrent.futures.ThreadPoolExecutor(max_workers=25) as executor:
                executor.map(lambda lid: _fetch_one(lid, session), work_items)

            if session_expired:
                # Save what we have before returning
                with lock:
                    _save_cache()
                self._update_status("Session expired — progress saved. Try again.")
                self._jwt_token = None
                return

            # ── Step 5: Final tab rebuild + save ──
            with lock:
                _save_cache()
            played, unplayed = self._rebuild_data()



            self._update_status(
                f"Done — {played} played, {unplayed} unplayed "
                f"({errors} errors)"
            )

        except Exception as e:
            logger.exception("Error in _do_fetch_all")
            self._update_status(f"Error: {e}")
        finally:
            self.after(0, lambda: self._set_running(False))

    def _rebuild_data(self):
        """Build unified row list from current data and update the UI."""
        scenario_info = self._scenario_info
        user_by_lid = self._user_by_lid
        friends_by_lid = self._friends_by_lid
        rows = []
        played = 0
        unplayed = 0

        stats_dir = self._get_stats_dir()
        local_stats = _get_local_stats(stats_dir)
        now = datetime.datetime.now()
        entry_history = self._scores_cache.get("entry_history", {})

        SCENARIO_BLACKLIST = {
        }

        for lid, info in scenario_info.items():
            sname = info["name"]
            if sname in SCENARIO_BLACKLIST:
                continue

            has_user = lid in user_by_lid
            has_friends = lid in friends_by_lid
            lstats = local_stats.get(sname, {"count": 0, "last_played": None, "trend": 1.0})

            hist = entry_history.get(lid, {})
            popularity_trend = 0.0
            if hist:
                dates = sorted(hist.keys())
                if len(dates) >= 2:
                    try:
                        d0_str = dates[0] if len(dates[0]) > 10 else dates[0] + "T00:00:00"
                        oldest = datetime.datetime.fromisoformat(d0_str)
                        d1_str = dates[-1] if len(dates[-1]) > 10 else dates[-1] + "T00:00:00"
                        newest = datetime.datetime.fromisoformat(d1_str)
                        
                        seconds_diff = (newest - oldest).total_seconds()
                        if seconds_diff >= 1800:  # Need at least 30 minutes to project steady trend
                            days_diff = seconds_diff / 86400.0
                            entry_diff = hist[dates[-1]] - hist[dates[0]]
                            popularity_trend = float(entry_diff) / days_diff
                    except ValueError:
                        pass

            competition_multiplier = max(0.2, math.log10(max(1.0, popularity_trend + 1.0)) / 2.0)

            row = {
                "Scenario": sname,
                "Entry Count": str(info["entries"]),
                "New Entries / Day": f"{popularity_trend:.1f}" if popularity_trend > 0 else "0.0",
                "Trend Mult": f"{competition_multiplier:.2f}x",
                "Local Runs": str(lstats["count"]),
                "Utility": "",
            }

            if has_user or has_friends:
                played += 1
                best = None
                if has_friends:
                    for fr in friends_by_lid[lid]:
                        try:
                            frank = int(fr["rank"])
                        except (ValueError, TypeError):
                            frank = 999999
                        if best is None or frank < best[1]:
                            best = (fr["friend"], frank, fr["score"], fr.get("date", ""))

                row["My Rank"] = str(user_by_lid[lid]["rank"]) if has_user else ""
                row["My Score"] = str(user_by_lid[lid]["score"]) if has_user else ""
                row["Score Date"] = user_by_lid[lid].get("date", "") if has_user else ""
                row["Best Friend"] = best[0] if best else ""
                row["Friend Rank"] = str(best[1]) if best else ""
                row["Friend Score"] = str(best[2]) if best else ""
                row["Friend Score Date"] = best[3] if best else ""

                if has_user and row["My Rank"] and row["Entry Count"]:
                    try:
                        rank = int(row["My Rank"])
                        entries = int(row["Entry Count"])
                        pct = (1 - rank / entries) * 100
                        row["Percentile"] = f"{pct:.2f}%"

                        # Calculate Utility Score (Optimized Algorithm)
                        # 1. Logarithmic Potential — neutralizes population bias
                        skill_gap = 1.0 - pct / 100.0
                        log_weight = math.log10(max(rank, 10))
                        base_potential = log_weight * skill_gap

                        # 2. Spaced Repetition (Time Factor) — Ebbinghaus curve
                        if lstats.get("last_played"):
                            days_ago = (now - lstats["last_played"]).total_seconds() / 86400.0
                            time_factor = 0.8 + 0.7 * (1.0 - math.exp(-max(0, days_ago) / 14.0))
                        else:
                            time_factor = 1.5  # Maximum priority for unplayed benchmarks

                        # 3. Session Fatigue — decoupled from PB tracking (fixes min() bug)
                        runs_today = lstats.get("runs_today", 0)
                        fatigue_factor = math.exp(-runs_today / 12.0)

                        # 4. Variance-Modulated Plateau Penalty (Sigmoid Decay)
                        pb_ago = lstats.get("runs_since_recent_pb", 0)
                        trend = lstats.get("trend", 1.0)
                        if trend <= 1.02:
                            # Sigmoid: max 85% penalty, inflection at 20 runs
                            plateau_penalty = 1.0 - (0.85 / (1.0 + math.exp(-0.4 * (pb_ago - 20.0))))
                        else:
                            plateau_penalty = 1.0

                        # 5. Active Learning Bonus — clamped trend factor
                        trend_factor = max(0.8, min(trend, 1.3))

                        # 6. Final Utility — *1000 converts small log floats to readable ints
                        utility = (base_potential * 1000) * time_factor * fatigue_factor * plateau_penalty * trend_factor * competition_multiplier
                        row["Utility"] = f"{int(utility)}"

                    except (ValueError, TypeError, ZeroDivisionError):
                        row["Percentile"] = ""
                else:
                    row["Percentile"] = ""

                if best and row["Entry Count"]:
                    try:
                        fpct = (1 - best[1] / int(row["Entry Count"])) * 100
                        row["Friend Percentile"] = f"{fpct:.2f}%"
                    except (ValueError, TypeError, ZeroDivisionError):
                        row["Friend Percentile"] = ""
                else:
                    row["Friend Percentile"] = ""

                rank_diff = ""
                if has_user and best:
                    try:
                        rank_diff = str(int(row["My Rank"]) - best[1])
                    except (ValueError, TypeError):
                        pass
                row["Rank Diff"] = rank_diff

                pctile_diff = ""
                if row["Percentile"] and row["Friend Percentile"]:
                    try:
                        my_pct = float(row["Percentile"].rstrip("%"))
                        fr_pct = float(row["Friend Percentile"].rstrip("%"))
                        pctile_diff = f"{my_pct - fr_pct:+.2f}%"
                    except (ValueError, TypeError):
                        pass
                row["Pctile Diff"] = pctile_diff
            else:
                unplayed += 1
                row["My Rank"] = ""
                row["My Score"] = ""
                row["Percentile"] = ""
                row["Score Date"] = ""
                row["Best Friend"] = ""
                row["Friend Rank"] = ""
                row["Friend Score"] = ""
                row["Friend Percentile"] = ""
                row["Friend Score Date"] = ""
                row["Rank Diff"] = ""
                row["Pctile Diff"] = ""

            rows.append(row)

        self._all_data = rows
        self._apply_current_sort()
        self.after(0, lambda: self._apply_filter())
        return played, unplayed

    # -------------------------------------------------------------------
    # Thread-safe helpers
    # -------------------------------------------------------------------
    def _update_status(self, msg):
        logger.info(msg)
        self.after(0, lambda: self._status_var.set(msg))

    # -------------------------------------------------------------------
    # Column visibility
    # -------------------------------------------------------------------
    def _get_auto_hidden_cols(self):
        """Return the set of columns auto-hidden by the current filter."""
        if self._filters:
            active = [k for k, v in self._filters.items() if v.get()]
            if len(active) == 1:
                return FILTER_HIDDEN_COLS.get(active[0], set())
        return set()

    def _show_column_menu(self, event):
        """Show a context menu to toggle column visibility."""
        auto_hidden = self._get_auto_hidden_cols()
        menu = tk.Menu(self, tearoff=0, bg=BG_LIGHTER, fg=TEXT,
                       activebackground=ACCENT, activeforeground="#fff",
                       font=("Segoe UI", 9),
                       disabledforeground=TEXT_DIM)
        for col_name, var in self._visible_cols.items():
            if col_name in auto_hidden:
                menu.add_checkbutton(
                    label=f"{col_name}  (filtered)", variable=var,
                    state="disabled")
            else:
                menu.add_checkbutton(
                    label=col_name, variable=var,
                    command=self._refresh_columns)
        menu.tk_popup(event.x_root, event.y_root)

    def _refresh_columns(self, save=True):
        """Update displaycolumns based on visibility toggles and active filters."""
        auto_hidden = self._get_auto_hidden_cols()

        visible = ["▶", "Scenario"]
        for col_name, var in self._visible_cols.items():
            if var.get() and col_name not in auto_hidden:
                visible.append(col_name)
        self._tree.configure(displaycolumns=visible)
        self._schedule_autofit()
        if save:
            self._cfg["visible_columns"] = [
                c for c, v in self._visible_cols.items() if v.get()
            ]
            save_config(self._cfg)

    # -------------------------------------------------------------------
    # Log panel
    # -------------------------------------------------------------------
    def _setup_log_redirect(self):
        sys.stdout = StdoutRedirector(self._append_log, sys.__stdout__)
        sys.stderr = StdoutRedirector(self._append_log, sys.__stderr__)

        gui_handler = logging.Handler()
        gui_handler.setLevel(logging.DEBUG)
        gui_handler.emit = lambda record: self._append_log(
            f"[{record.levelname}] {record.getMessage()}")
        logger.addHandler(gui_handler)

    def _append_log(self, text):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {text}\n"
        def _do():
            self._log_text.configure(state="normal")
            self._log_text.insert("end", line)
            self._log_text.configure(state="disabled")
            self._log_text.see("end")
        self.after(0, _do)

    def _toggle_log(self):
        if self._log_visible:
            self._log_text_container.pack_forget()
            self._log_resize_handle.pack_forget()
            self._log_toggle_btn.configure(text="▶ Log")
        else:
            # Re-pack resize handle at top, then text container
            self._log_resize_handle.pack(fill="x", before=self._log_header)
            self._log_text_container.pack(fill="both", padx=4, pady=(0, 4))
            self._log_text_container.configure(height=self._log_height)
            self._log_text_container.pack_propagate(False)
            self._log_toggle_btn.configure(text="▼ Log")
            # Scroll to bottom when opening
            self._log_text.see("end")
        self._log_visible = not self._log_visible

    def _log_resize_start(self, event):
        """Begin resizing the log panel."""
        self._log_drag_start_y = event.y_root
        self._log_drag_start_h = self._log_text_container.winfo_height()

    def _log_resize_drag(self, event):
        """Handle drag to resize the log panel."""
        if self._log_drag_start_y is None:
            return
        dy = self._log_drag_start_y - event.y_root  # dragging up = bigger
        new_h = self._log_drag_start_h + dy
        new_h = max(self._log_min_height, min(self._log_max_height, new_h))
        self._log_height = new_h
        self._log_text_container.configure(height=new_h)

    def _clear_log(self):
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    # -------------------------------------------------------------------
    # Auto-update polling
    # -------------------------------------------------------------------
    def _start_stats_polling(self):
        stats_dir = self._get_stats_dir()
        if not os.path.exists(stats_dir):
            return
        try:
            for f in os.listdir(stats_dir):
                if f.endswith(" Stats.csv"):
                    self._known_stat_files.add(f)
        except OSError:
            pass
        self._poll_stats_folder()

    def _poll_stats_folder(self):
        stats_dir = self._get_stats_dir()
        try:
            current_files = set(f for f in os.listdir(stats_dir) if f.endswith(" Stats.csv"))
            new_files = current_files - self._known_stat_files
            if new_files:
                self._known_stat_files.update(new_files)
                threading.Thread(target=self._handle_new_stats_files, args=(stats_dir, new_files,), daemon=True).start()
        except OSError:
            pass
        self._poll_stats_id = self.after(5000, self._poll_stats_folder)
        
    def _handle_new_stats_files(self, stats_dir, new_files):
        snames = set()
        lids_to_update = {}  # lid -> expected_new_score
        
        for fname in new_files:
            base = fname[:-10]
            parts = base.rsplit(" - ", 2)
            if len(parts) >= 3:
                sname = parts[0]
                snames.add(sname)
                
                # Try to parse the score from this new run
                fpath = os.path.join(stats_dir, fname)
                score_val = None
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if line.startswith("Score:,"):
                                score_val = float(line.split(",")[1])
                                break
                except Exception:
                    pass
                    
                for lid, info in self._scenario_info.items():
                    if info["name"] == sname:
                        if score_val is not None:
                            lids_to_update[lid] = max(lids_to_update.get(lid, -999999.0), score_val)
                        elif lid not in lids_to_update:
                            lids_to_update[lid] = -999999.0
                        break

        # Autoplay: advance immediately on local score detection (before API calls)
        if self._autoplay_var.get() and self._autoplay_current_scenario:
            if snames & {self._autoplay_current_scenario}:
                logger.info("Autoplay: local score detected for '%s', advancing…",
                            self._autoplay_current_scenario)
                # Capture next scenario NOW while the row still exists in the view
                self.after(0, self._autoplay_advance)

        if not lids_to_update:
            return
            
        # Give client a few seconds to upload the stats first
        time.sleep(3)
            
        if not self._jwt_token:
            username = self._cfg.get("username", "").strip()
            password = self._cfg.get("password", "").strip()
            if not username or not password:
                return
            try:
                self._jwt_token = kovaaks_login(username, password)
            except Exception as e:
                logger.debug("Failed silent login during stats poll: %s", e)
                return
                
        updated = False
        session = requests.Session()
        for lid, expected_score in lids_to_update.items():
            max_attempts = 5
            for attempt in range(max_attempts):
                try:
                    data = kovaaks_get_friends_scores(self._jwt_token, lid, session=session)
                    
                    friend_entries = []
                    user_entry = None
                    username = self._cfg.get("username", "").strip()
                    
                    for entry in data:
                        name = entry.get("webappUsername") or entry.get("steamAccountName", "")
                        if name.lower() == username.lower():
                            epoch = entry.get("attributes", {}).get("epoch", "")
                            score_date = ""
                            if epoch:
                                try:
                                    score_date = datetime.datetime.fromtimestamp(int(epoch) / 1000).strftime("%Y-%m-%d")
                                except (ValueError, TypeError, OSError):
                                    pass
                            user_entry = {
                                "rank": entry.get("rank", ""),
                                "score": entry.get("score", ""),
                                "date": score_date,
                            }
                        else:
                            f_epoch = entry.get("attributes", {}).get("epoch", "")
                            f_date = ""
                            if f_epoch:
                                try:
                                    f_date = datetime.datetime.fromtimestamp(int(f_epoch) / 1000).strftime("%Y-%m-%d")
                                except (ValueError, TypeError, OSError):
                                    pass
                            friend_entries.append({
                                "friend": name,
                                "rank": entry.get("rank", ""),
                                "score": entry.get("score", ""),
                                "date": f_date,
                            })
                    
                    target_met = True
                    if user_entry and expected_score > -999999.0:
                        try:
                            # Clean up the score string the same way display sorting does
                            clean_score = str(user_entry["score"]).replace(",", "")
                            if clean_score.endswith("%"):
                                clean_score = clean_score[:-1]
                            api_score = float(clean_score)
                            
                            if api_score < expected_score:
                                target_met = False
                        except (ValueError, TypeError):
                            pass
                            
                    if target_met or attempt == max_attempts - 1:
                        if user_entry:
                            self._user_by_lid[lid] = user_entry
                        if friend_entries:
                            self._friends_by_lid[lid] = friend_entries
                            
                        if lid not in self._scores_cache.setdefault("scores", {}):
                            self._scores_cache["scores"][lid] = {}
                        if user_entry:
                            self._scores_cache["scores"][lid]["user"] = user_entry
                        if friend_entries:
                            self._scores_cache["scores"][lid]["friends"] = friend_entries
                            
                        updated = True
                        logger.info("Auto-updated scores for scenario lid=%s", lid)
                        break
                    else:
                        logger.debug("Score for lid=%s not updated yet in API, retrying (%d/%d)...", lid, attempt+1, max_attempts)
                        time.sleep(4)
                except Exception as e:
                    logger.debug("Failed auto-update for lid=%s on attempt %d: %s", lid, attempt+1, e)
                    time.sleep(4)
                
        if updated:
            try:
                with open(SCORES_CACHE, "w", encoding="utf-8") as f:
                    json.dump(self._scores_cache, f, separators=(",", ":"))
            except OSError:
                pass
            self.after(0, self._rebuild_data)

    # -------------------------------------------------------------------
    # Autoplay
    # -------------------------------------------------------------------
    def _toggle_autoplay(self):
        """Toggle autoplay mode on/off."""
        self._autoplay_var.set(not self._autoplay_var.get())
        if self._autoplay_var.get():
            self._autoplay_btn.configure(bg=GREEN, fg="#fff")
            self._autoplay_btn.unbind("<Enter>")
            self._autoplay_btn.unbind("<Leave>")
            self._autoplay_btn.bind("<Enter>",
                lambda e: self._autoplay_btn.configure(bg=GREEN_HOVER))
            self._autoplay_btn.bind("<Leave>",
                lambda e: self._autoplay_btn.configure(bg=GREEN))
            # Start tracking from the currently selected row, or first row
            sel = self._tree.selection()
            if sel:
                vals = self._tree.item(sel[0], "values")
                if vals and len(vals) > 1:
                    self._autoplay_current_scenario = vals[1]
            else:
                children = self._tree.get_children()
                if children:
                    vals = self._tree.item(children[0], "values")
                    if vals and len(vals) > 1:
                        self._autoplay_current_scenario = vals[1]
                        self._tree.selection_set(children[0])
                        self._tree.see(children[0])
            if self._autoplay_current_scenario:
                self._update_status(
                    f"Autoplay ON — waiting for score on: "
                    f"{self._autoplay_current_scenario}")
            else:
                self._update_status("Autoplay ON — select or play a scenario to begin")
        else:
            self._autoplay_btn.configure(bg=BG_LIGHTER, fg=TEXT_DIM)
            self._autoplay_btn.unbind("<Enter>")
            self._autoplay_btn.unbind("<Leave>")
            self._autoplay_current_scenario = None
            self._update_status("Autoplay OFF")

    def _autoplay_advance(self):
        """Advance to the next scenario in the treeview and launch it."""
        if not self._autoplay_var.get():
            return

        tree = self._tree
        children = tree.get_children()
        if not children:
            self._update_status("Autoplay: no scenarios in list")
            return

        # Find the row matching the current autoplay scenario
        current_idx = None
        for i, iid in enumerate(children):
            vals = tree.item(iid, "values")
            if vals and len(vals) > 1 and vals[1] == self._autoplay_current_scenario:
                current_idx = i
                break

        if current_idx is None:
            # Current scenario not in view — just stay
            self._update_status(
                f"Autoplay: '{self._autoplay_current_scenario}' not found in current view")
            return

        next_idx = current_idx + 1
        if next_idx >= len(children):
            # Reached end of list
            self._autoplay_var.set(False)
            self._autoplay_btn.configure(bg=BG_LIGHTER, fg=TEXT_DIM)
            self._autoplay_btn.unbind("<Enter>")
            self._autoplay_btn.unbind("<Leave>")
            self._autoplay_current_scenario = None
            self._update_status("Autoplay: reached end of list, autoplay disabled")
            return

        next_iid = children[next_idx]
        next_vals = tree.item(next_iid, "values")
        if not next_vals or len(next_vals) < 2:
            return

        next_scenario = next_vals[1]
        self._autoplay_current_scenario = next_scenario

        # Select, scroll to, and launch the next scenario
        tree.selection_set(next_iid)
        tree.see(next_iid)

        uri = STEAM_LAUNCH_URI.format(urllib.parse.quote(next_scenario, safe=""))
        webbrowser.open(uri)
        self._update_status(
            f"Autoplay: launching '{next_scenario}' — waiting for score…")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = KovaaksApp()
    app.mainloop()
