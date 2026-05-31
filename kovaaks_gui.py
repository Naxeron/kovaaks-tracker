#!/usr/bin/env python3
"""
KovaaKs Scenario Tracker — Dark-themed GUI
Two tabs: Played (user + friends) and Unplayed.
Only considers scenarios with customizable minimum leaderboard entries (default 1000).
"""

import tkinter as tk
from tkinter import ttk, messagebox
import json
import os
import sys
import logging
import threading
import time
import datetime
import math
import urllib.parse
import webbrowser
import concurrent.futures
import gzip
import io

import requests
import queue

# ---------------------------------------------------------------------------
# Extracted modules
# ---------------------------------------------------------------------------
from kovaaks.constants import (
    BG, BG_DARKER, BG_LIGHTER, ACCENT, ACCENT_HOVER, TEXT, TEXT_DIM,
    ENTRY_BG, TREE_BG, TREE_FG, TREE_SEL_BG, TREE_SEL_FG,
    LOG_BG, LOG_FG, ALT_ROW, HEADER_BG, BORDER, GREEN, GREEN_HOVER,
    COLUMNS, FILTER_HIDDEN_COLS, MIN_ENTRIES,
    SCENARIO_DISTRIBUTION_POINTS, SCENARIO_POPULARITY_DROP_OFF_POINTS,
    GITHUB_RAW_BASE, STEAM_LAUNCH_URI,
    LAUNCH_MARKER as _LAUNCH_MARKER,
)
from kovaaks.api import (
    api_request_with_retry as _api_request_with_retry,
    KOVAAKS_HEADERS as _KOVAAKS_HEADERS,
    get_accurate_entry_count as _get_accurate_entry_count,
    kovaaks_login,
    kovaaks_get_friends_scores,
    fetch_all_scenarios,
)
from kovaaks.cache import (
    load_scenarios_from_cache,
    load_scores_cache,
    save_scores_cache,
    SCORES_CACHE,
)
from kovaaks.stats import get_local_stats as _get_local_stats
from kovaaks.config_helpers import (
    load_config,
    save_config,
    get_default_stats_dir,
    CONFIG_PATH,
)
from kovaaks.data_processing import (
    get_estimated_fetch_count,
    get_estimated_matching_count,
    natural_sort_key,
    parse_leaderboard_entries,
    safe_int,
)
from kovaaks.dialogs import PasswordDialog, SettingsDialog, _bind_entry_ctrl_a, ToolTip
from kovaaks.fetch_worker import run_fetch_all
from kovaaks.logging_helpers import setup_logging, StdoutRedirector

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logger = setup_logging()


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class KovaaksApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self._gui_queue = queue.Queue()
        self._process_gui_queue()
        self.title("KovaaKs Scenario Tracker")
        self.geometry("1280x780")
        self.minsize(900, 500)
        self.configure(bg=BG)

        self._cfg = load_config()
        self._hidden_scenarios = set(self._cfg.get("hidden_scenarios", []))

        self._all_data: list[dict] = []
        self._tree: ttk.Treeview | None = None
        self._filter_var = tk.StringVar()
        self._count_var = tk.StringVar(value="")
        self._points_var = tk.StringVar(value="")
        self._potential_var = tk.StringVar(value="")
        self._projected_gain_var = tk.StringVar(value="")
        self._next_rank_var = tk.StringVar(value="")
        self._unplayed_needed_var = tk.StringVar(value="")
        self._next_global_points = None
        self._fetching_next_rank = False
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
        self._global_points_sum = 0
        self._global_potential_points_sum = 0
        self._global_projected_gain_sum = 0

        # Thread safety: lock for shared data structures
        self._data_lock = threading.Lock()

        # Local stats cache to avoid re-scanning disk every rebuild
        self._local_stats_cache: dict = {}
        self._local_stats_dirty: bool = True

        # Autoplay state
        self._autoplay_var = tk.BooleanVar(value=False)
        self._autoplay_current_scenario: str | None = None
        self._autoplay_btn: tk.Button | None = None

        # Progress bar animation state
        self._current_progress = 0.0
        self._target_progress = 0.0
        self._animating_progress = False

        self._apply_styles()
        self._build_ui()
        self._setup_log_redirect()

        # Load cache and populate view immediately
        self._load_cache_and_populate()

        # Authentication on startup
        self._password = None
        if not self._cfg.get("username"):
            self.after(300, self._on_settings)
        else:
            # Prompt for password if username is configured, so background sync is ready
            self.after(500, self._get_password)

        self._start_stats_polling()
        logger.info("Application started")

        # Auto-fit columns on window resize
        self.bind("<Configure>", self._on_window_resize)
        
        # Schedule auto refresh
        self._auto_refresh_id = None
        if self._cfg.get("auto_refresh", False):
            # Initial check after 10 seconds, then periodic
            self.after(10000, self._auto_refresh_step)

    def _get_stats_dir(self):
        path = self._cfg.get("stats_dir", get_default_stats_dir())
        return os.path.expanduser(path) if path else ""

    def _get_password(self):
        """Prompt for password if not available in memory."""
        if self._password:
            return self._password
        
        username = self._cfg.get("username", "").strip()
        if not username:
            self._on_settings()
            return None
            
        dlg = PasswordDialog(self, username)
        self.wait_window(dlg)
        if dlg.result:
            self._password = dlg.result
            return self._password
        return None

    def _check_github_updates(self):
        """Lightweight check to see if GitHub files have changed via ETags/Last-Modified."""
        urls = [
            f"{GITHUB_RAW_BASE}/scenarios.json.gz",
            f"{GITHUB_RAW_BASE}/scenarios_history.json.gz"
        ]
        
        last_etags = self._cfg.get("last_etags", {})
        any_changed = False
        new_etags = last_etags.copy()
        
        try:
            for url in urls:
                key = url.split("/")[-1]
                logger.debug("Checking GitHub update for %s...", key)
                # Use HEAD request to check headers only with limited retries
                # to avoid long blocking when GitHub is flaky
                resp = _api_request_with_retry("head", url, timeout=10, max_retries=3)
                if resp.status_code == 200:
                    etag = resp.headers.get("ETag")
                    last_modified = resp.headers.get("Last-Modified")
                    
                    # Store both ETag and Last-Modified for robustness
                    current_marker = etag or last_modified
                    
                    if current_marker != last_etags.get(key):
                        logger.info("Update detected for %s: %s -> %s", key, last_etags.get(key), current_marker)
                        any_changed = True
                        new_etags[key] = current_marker
                    else:
                        logger.debug("%s is up to date (%s)", key, current_marker)
                else:
                    logger.warning("GitHub HEAD request failed for %s: %d", url, resp.status_code)
                    # If we can't check, assume it might have changed to be safe
                    any_changed = True
        except Exception as e:
            logger.warning("Error checking for GitHub updates: %s", e)
            return True # Assume changed on error
            
        if any_changed:
            self._cfg["last_etags"] = new_etags
            
        return any_changed

    def _auto_refresh_step(self):
        if self._cfg.get("auto_refresh", False) and not self._running:
            if self._cfg.get("auto_refresh_github_only", False):
                self._set_running(True, "Checking for GitHub updates...", disable_ui=False)
                threading.Thread(target=self._do_background_github_check, daemon=True).start()
                return

            username = self._cfg.get("username", "").strip()
            if username:
                # Always trigger fetch; _on_fetch_all and _do_fetch_all will handle 
                # skipping scores if password is missing
                self._on_fetch_all(force_login=False)
        
        self._schedule_next_auto_refresh()

    def _do_background_github_check(self):
        """Run the GitHub check in a background thread to prevent UI freezing."""
        try:
            changed = self._check_github_updates()
        except Exception as e:
            logger.warning("Error during background GitHub updates check: %s", e)
            changed = True  # Safe fallback
        
        self.run_in_gui_thread(self._on_background_github_check_complete, changed)

    def _on_background_github_check_complete(self, changed):
        """Handle the result of the background GitHub check on the main UI thread."""
        self._set_running(False)
        
        if not self._cfg.get("auto_refresh", False):
            self._update_status("Ready")
            return

        if not changed:
            logger.info("Auto-refresh skipped: No updates on GitHub.")
            if self._scenario_info:
                # Offload history recording and cache saving to a background
                # thread — both are CPU/IO heavy and would freeze the GUI.
                threading.Thread(
                    target=self._save_history_in_background,
                    daemon=True,
                ).start()
            self._update_status("Ready (GitHub up-to-date)")
            self._schedule_next_auto_refresh()
        else:
            username = self._cfg.get("username", "").strip()
            if username:
                self._on_fetch_all(force_login=False)
            else:
                self._schedule_next_auto_refresh()

    def _save_history_in_background(self):
        """Record history points and save cache off the main thread."""
        try:
            fake_master = [
                {"leaderboardId": lid, "counts": {"entries": info.get("entries", 0)}}
                for lid, info in self._scenario_info.items()
            ]
            self._record_history_points(fake_master)
            save_scores_cache(self._scores_cache)
        except Exception as e:
            logger.warning("Error saving history in background: %s", e)

    def _schedule_next_auto_refresh(self):
        if self._auto_refresh_id:
            self.after_cancel(self._auto_refresh_id)
            self._auto_refresh_id = None
            
        if not self._cfg.get("auto_refresh", False):
            return

        interval_min = 60
        if self._cfg.get("auto_refresh_github_only", False):
            # If waiting for github data update, check every minute
            interval_min = 1
        else:
            try:
                interval_min = int(self._cfg.get("refresh_interval", "60"))
            except ValueError:
                pass
        if interval_min < 1: interval_min = 1
        
        self._auto_refresh_id = self.after(interval_min * 60000, self._auto_refresh_step)

    # -------------------------------------------------------------------
    # Styles
    # -------------------------------------------------------------------
    def _apply_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Dark.Treeview", background=TREE_BG, foreground=TREE_FG, fieldbackground=TREE_BG, rowheight=28, font=("Segoe UI", 10), borderwidth=0)
        style.configure("Dark.Treeview.Heading", background=HEADER_BG, foreground=TEXT, font=("Segoe UI", 10, "bold"), borderwidth=1, relief="flat")
        style.map("Dark.Treeview", background=[("selected", TREE_SEL_BG)], foreground=[("selected", TREE_SEL_FG)])
        style.map("Dark.Treeview.Heading", background=[("active", ACCENT)])
        style.configure("Dark.TFrame", background=BG)
        style.configure("Dark.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Status.TLabel", background=BG_DARKER, foreground=TEXT_DIM, font=("Segoe UI", 9), padding=[8, 4])
        style.configure("Title.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI", 18, "bold"))
        style.configure("Dark.Vertical.TScrollbar", background=BG_LIGHTER, troughcolor=BG_DARKER, arrowcolor=TEXT)

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

        # Autoplay toggle button
        self._autoplay_btn = self._make_button(ctrl, "🔁 Autoplay", self._toggle_autoplay)
        self._autoplay_btn.pack(side="left", padx=4)

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
        _bind_entry_ctrl_a(filter_entry)
        self._filter_var.trace_add("write", lambda *_a: self._apply_filter())

        count_label = ttk.Label(filter_frame, textvariable=self._count_var,
                                style="Dark.TLabel")
        count_label.pack(side="right", padx=8)

        potential_label = ttk.Label(filter_frame, textvariable=self._potential_var,
                                     style="Dark.TLabel")
        potential_label.pack(side="right", padx=8)

        points_label = ttk.Label(filter_frame, textvariable=self._points_var,
                                 style="Dark.TLabel")
        points_label.pack(side="right", padx=8)

        next_rank_label = ttk.Label(filter_frame, textvariable=self._next_rank_var,
                                    style="Dark.TLabel")
        next_rank_label.pack(side="right", padx=8)

        self._tooltips = [
            ToolTip(count_label, self._get_count_tooltip),
            ToolTip(points_label, self._get_points_tooltip),
            ToolTip(potential_label, self._get_potential_tooltip),
            ToolTip(next_rank_label, self._get_next_rank_tooltip)
        ]

        # Toggle filter buttons
        toggle_frame = tk.Frame(content, bg=BG, padx=12)
        toggle_frame.pack(fill="x")

        for filter_key, label_text in [
            ("losing", "👎 Losing"),
            ("friends_only", "👥 Friends Only"),
            ("me_only", "🙋 Me Only"),
            ("unplayed", "❌ Unplayed"),
            ("hidden", "👁️ Hidden"),
        ]:
            var = tk.BooleanVar(value=False)
            self._filters[filter_key] = var
            if filter_key == "hidden":
                self._make_toggle_button(toggle_frame, label_text, var, on_toggle=self._rebuild_data)
            else:
                self._make_toggle_button(toggle_frame, label_text, var)

        # Spacer
        tk.Frame(toggle_frame, bg=BG, width=16).pack(side="left")



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

        # Right-click → context menus
        self._tree.bind("<Button-3>", self._on_right_click)

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
        clear_btn.pack(side="right")

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

        # — Progress Bar (Sleek Custom) —
        self._progress_bg = tk.Frame(self, bg=BG_DARKER, height=5)
        self._progress_bg.pack(fill="x", side="bottom")
        self._progress_fill = tk.Frame(self._progress_bg, bg=ACCENT, height=5)
        self._progress_fill.place(x=0, y=0, relwidth=0.0, relheight=1.0)

        # Shimmer highlight
        self._progress_shimmer = tk.Frame(self._progress_fill, bg=ACCENT_HOVER, height=5)
        self._progress_shimmer.place(relx=-0.5, relwidth=0.4, relheight=1.0)
        self.after(500, self._shimmer_loop)

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

    def _make_toggle_button(self, parent, text, var, on_toggle=None):
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
            if on_toggle:
                on_toggle()
            else:
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

        # Sample a subset of rows for performance (first, last, and evenly spaced)
        n = len(children)
        if n <= 200:
            sample = children
        else:
            step = max(1, n // 100)
            indices = set(range(0, n, step))
            indices.add(0)
            indices.add(n - 1)
            sample = [children[i] for i in sorted(indices)]

        for col_name, default_width in COLUMNS:
            if col_name == "▶":
                continue
            longest = ""
            for iid in sample:
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
        self._scores_cache = load_scores_cache()

        all_scenarios = load_scenarios_from_cache(self._scores_cache)
        if not all_scenarios:
            return

        # Filter to config-defined min entries
        min_entries_threshold = int(self._cfg.get("min_entries", MIN_ENTRIES))
        master = []
        for s in all_scenarios:
            entries = s.get("counts", {}).get("entries", 0)
            try:
                entries = int(entries)
            except (ValueError, TypeError):
                entries = 0
            if entries >= min_entries_threshold:
                master.append(s)

        if not master:
            return

        # Build lid -> scenario info map
        scenario_info = {}
        for s in master:
            lid = str(s.get("leaderboardId", ""))
            scenario_info[lid] = {
                "name": s.get("scenarioName", ""),
                "entries": s.get("counts", {}).get("entries", ""),
                "aimType": s.get("scenario", {}).get("aimType"),
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
        self._update_progress(1, 1)

    # -------------------------------------------------------------------
    # Settings
    # -------------------------------------------------------------------
    def _on_settings(self):
        old_stats_dir = self._cfg.get("stats_dir")
        dlg = SettingsDialog(self, self._cfg)
        self.wait_window(dlg)
        if dlg.result:
            new_password = dlg.result.pop("password", None)
            if new_password:
                self._password = new_password
            self._cfg.update(dlg.result)
            save_config(self._cfg)
            self._update_status("Settings saved.")
            self._apply_filter()

            # Update auto-refresh timer
            self._schedule_next_auto_refresh()

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
    def _get_count_tooltip(self):
        if not hasattr(self, "_current_stats"):
            return "Count of scenarios currently shown in the table."
        s = self._current_stats
        return (f"Shown Scenarios: {s['total_count']:,}\n"
                f"  • Played: {s['played_count']:,}\n"
                f"  • Unplayed: {s['unplayed_count']:,}")

    def _get_points_tooltip(self):
        if not hasattr(self, "_current_stats"):
            return "Points:\nSum of (Total Entries - Your Rank) for all shown scenarios.\n1 Point = 1 Player beaten."
        
        s = self._current_stats
        is_global = self._cfg.get("always_show_total_points", True)
        
        text = ""
        if is_global:
            text += f"Global Points (All Scenarios): {self._global_points_sum:,}\n\n"
            text += f"Filtered Points (Shown Only): {s['total_points']:,}\n"
        else:
            text += f"Filtered Points (Shown Only): {s['total_points']:,}\n"
        return text.strip()

    def _get_potential_tooltip(self):
        if not hasattr(self, "_current_stats"):
            return "Potential Points:\nSum of (Your Rank - 1) for played scenarios,\nor (Total Entries - 1) for unplayed scenarios."
        
        s = self._current_stats
        is_global = self._cfg.get("always_show_total_points", True)
        
        text = ""
        if is_global:
            text += f"Global Potential (All Scenarios): {self._global_potential_points_sum:,}\n\n"
            text += f"Filtered Potential (Shown Only): {s['total_potential']:,}\n"
        else:
            text += f"Filtered Potential (Shown Only): {s['total_potential']:,}\n"
            
        text += (f"How it's calculated (for shown scenarios):\n"
                 f"  From Played Scenarios: {s['total_rank'] - s['played_count']:,}\n"
                 f"  From Unplayed Scenarios: {s['unplayed_entries'] - s['unplayed_count']:,}")
                 
        text += "\n\n" + "─"*30 + "\n\n"
        text += self._get_projected_gain_tooltip()
        return text

    def _get_projected_gain_tooltip(self):
        text = ("Projected Gain:\n"
                "Estimated points gained if you reach your average percentile for each scenario's Aim Type.\n\n")
        
        if hasattr(self, "_current_stats"):
            s = self._current_stats
            is_global = self._cfg.get("always_show_total_points", True)
            if is_global:
                text += f"Global Projected Gain: {self._global_projected_gain_sum:,}\n"
                text += f"Filtered Projected Gain (Shown Only): {s['total_projected']:,}\n\n"
            else:
                text += f"Filtered Projected Gain (Shown Only): {s['total_projected']:,}\n\n"
                
        text += "Your Averages:\n"
        
        if not hasattr(self, "_aim_type_avgs") or not self._aim_type_avgs:
            return text + "No data yet."
            
        for atype, pct in sorted(self._aim_type_avgs.items()):
            text += f"• {atype}: {pct:.1f}th Percentile\n"
            
        return text.strip()

    def _get_next_rank_tooltip(self):
        text = "Next Rank:\n"
        if self._next_global_points is None:
            return text + "Points needed to overtake the player above you on the global leaderboard.\nLoading..."
        
        diff = self._next_global_points - self._global_points_sum
        text += f"You need {diff:,} more points to rank up globally."
        
        text += "\n\n" + "─"*30 + "\n\n"
        unplayed_val = self._unplayed_needed_var.get()
        if unplayed_val:
            text += f"{unplayed_val}\n"
        text += "Based on your projected averages,\nwe calculate how many more unplayed scenarios you need to play."
        return text

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
        stats = {
            "total_count": len(rows),
            "played_count": 0,
            "unplayed_count": 0,
            "total_points": 0,
            "total_potential": 0,
            "total_projected": 0,
            "played_entries": 0,
            "unplayed_entries": 0,
            "total_rank": 0
        }

        for i, row in enumerate(rows):
            values = [row.get(c, "") for c in cols]
            values[0] = "▶"  # Play icon in first column
            tag = "odd" if i % 2 else "even"
            item = self._tree.insert("", "end", values=values, tags=(tag,))
            
            if len(values) > 1 and values[1] in selected_scenarios:
                items_to_select.append(item)
                
            try:
                stats["total_projected"] += float(row.get("_projected_gain", 0))
                rank = row.get("My Rank", "")
                entries = row.get("Entry Count", "")
                if entries:
                    e_val = int(entries)
                    if rank:
                        r_val = int(rank)
                        stats["total_points"] += (e_val - r_val)
                        stats["total_potential"] += (r_val - 1)
                        stats["played_count"] += 1
                        stats["played_entries"] += e_val
                        stats["total_rank"] += r_val
                    else:
                        stats["total_potential"] += (e_val - 1)
                        stats["unplayed_count"] += 1
                        stats["unplayed_entries"] += e_val
            except (ValueError, TypeError):
                continue
                
        stats["total_projected"] = int(stats["total_projected"])
        self._current_stats = stats
                
        self._count_var.set(f"{len(rows)} rows")
        
        use_global = self._cfg.get("always_show_total_points", True)
        pts = self._global_points_sum if use_global else stats["total_points"]
        pot = self._global_potential_points_sum if use_global else stats["total_potential"]
        gain = self._global_projected_gain_sum if use_global else stats["total_projected"]

        self._points_var.set(f"Points: {pts:,}" if pts > 0 else "")
        self._potential_var.set(f"Potential Points: {pot:,}" if pot > 0 else "")
        self._projected_gain_var.set(f"Projected Gain: {gain:,}" if gain > 0 else "")

        self._schedule_autofit()
        
        if items_to_select:
            self._tree.selection_set(items_to_select)
        if yview:
            self.after(0, lambda: self._tree.yview_moveto(yview[0]))

    def _apply_filter(self):
        query = self._filter_var.get().lower().strip()
        all_rows = list(self._all_data)

        if self._filters:
            losing = self._filters["losing"].get()
            friends_only = self._filters["friends_only"].get()
            me_only = self._filters["me_only"].get()
            unplayed = self._filters["unplayed"].get()

            if losing or friends_only or me_only or unplayed:
                matched = set()
                for idx, r in enumerate(all_rows):
                    has_rank = r.get("My Rank", "") != ""
                    has_friend = r.get("Best Friend", "") != ""
                    rank_diff = r.get("Rank Diff", "")

                    if losing and has_rank and has_friend and rank_diff:
                        try:
                            if int(rank_diff) > 0: matched.add(idx)
                        except (ValueError, TypeError): pass
                    if friends_only and has_friend and not has_rank: matched.add(idx)
                    if me_only and has_rank and not has_friend: matched.add(idx)
                    if unplayed and not has_rank and not has_friend: matched.add(idx)
                all_rows = [all_rows[i] for i in sorted(matched)]

        if query:
            all_rows = [r for r in all_rows if any(query in str(v).lower() for v in r.values())]

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

        has_val = [r for r in self._all_data if str(r.get(column, "")).strip()]
        no_val = [r for r in self._all_data if not str(r.get(column, "")).strip()]
        has_val.sort(key=lambda r: natural_sort_key(r.get(column, "")), reverse=reverse)
        self._all_data = has_val + no_val

        for c in [col[0] for col in COLUMNS]:
            self._tree.heading(c, text=c + ((" ▼" if reverse else " ▲") if c == column else ""))

    # -------------------------------------------------------------------
    # Fetch All — unified fetch operation
    # -------------------------------------------------------------------
    def _set_running(self, running, status="Ready", disable_ui=True):
        self._running = running
        if disable_ui or not running:
            state = "disabled" if running else "normal"
            for btn in self._all_buttons:
                btn.configure(state=state)
        self._status_var.set(status)

    def _on_fetch_all(self, force_login=True):
        username = self._cfg.get("username", "").strip()
        if not username:
            messagebox.showwarning("Username required",
                                   "Please configure your KovaaKs username in Settings.")
            self._on_settings()
            return
            
        password = self._password
        if not password and force_login:
            password = self._get_password()
            if not password:
                return

        if self._running:
            return

        self._set_running(True, "Starting…")
        threading.Thread(target=run_fetch_all, args=(self, username, password),
                         daemon=True).start()

    def _fetch_next_rank_points(self):
        try:
            current_points = self._global_points_sum
            if current_points <= 0:
                self.after(0, lambda: (self._next_rank_var.set("Next Rank: N/A"), self._unplayed_needed_var.set("Unplayed Needed: N/A")))
                return
                
            username = self._cfg.get("username", "").strip()
            if not username:
                self.after(0, lambda: (self._next_rank_var.set("Next Rank: N/A (No Username)"), self._unplayed_needed_var.set("Unplayed Needed: N/A")))
                return

            try:
                import kovaaks.api as api
                next_points = api.get_next_leaderboard_position_points(username, current_points)
                if next_points and next_points > current_points:
                    self._next_global_points = next_points
                    self.after(0, self._update_next_rank_display)
                else:
                    self.after(0, lambda: (self._next_rank_var.set("Next Rank: Rank 1!"), self._unplayed_needed_var.set("Unplayed Needed: 0")))
            except Exception as e:
                logger.warning("Error fetching next rank points: %s", e)
                self.after(0, lambda: (self._next_rank_var.set("Next Rank: Error"), self._unplayed_needed_var.set("Unplayed Needed: Error")))
        finally:
            self._fetching_next_rank = False

    def _update_next_rank_display(self):
        if self._next_global_points is None:
            return
            
        diff = self._next_global_points - self._global_points_sum
        self._next_rank_var.set(f"Next Rank: +{diff:,}")
        
        # Calculate unplayed needed
        # We find the average projected gain for an unplayed scenario
        avg_gain = 0
        unplayed_count = 0
        for lid, info in self._scenario_info.items():
            if lid not in self._user_by_lid:
                try:
                    e_val = int(info["entries"])
                    expected_pct = self._aim_type_avgs.get(info.get("aimType"), 50.0)
                    expected_rank = max(1, int(e_val * (1.0 - expected_pct / 100.0)))
                    gain = e_val - expected_rank
                    if gain > 0:
                        avg_gain += gain
                        unplayed_count += 1
                except (ValueError, TypeError):
                    pass
                    
        if unplayed_count > 0 and avg_gain > 0:
            avg_per_unplayed = avg_gain / unplayed_count
            needed = int(math.ceil(diff / avg_per_unplayed))
            self._unplayed_needed_var.set(f"Unplayed Needed: {needed:,}")
        else:
            self._unplayed_needed_var.set("Unplayed Needed: N/A")

    def _rebuild_data_and_finish(self, errors=0):
        self._next_global_points = None
        played, unplayed = self._rebuild_data()
        self._update_status(
            f"Done — {played} played, {unplayed} unplayed "
            f"({errors} errors)"
        )
        self._update_progress(100, 100)



    def _rebuild_data(self):
        """Build unified row list from current data and update the UI."""
        scenario_info = self._scenario_info
        user_by_lid = self._user_by_lid
        friends_by_lid = self._friends_by_lid
        rows = []
        played = 0
        unplayed = 0
        self._global_points_sum = 0
        self._global_potential_points_sum = 0
        self._global_projected_gain_sum = 0

        aim_type_pcts = {}
        for lid, info in scenario_info.items():
            if (u_data := user_by_lid.get(lid)) and (entries := safe_int(info.get("entries", 0))) > 0:
                if (rank := safe_int(u_data.get("rank"))) is not None and (aim_type := info.get("aimType")):
                    aim_type_pcts.setdefault(aim_type, []).append((1 - rank / entries) * 100)

        aim_type_avgs = {atype: sum(pcts) / len(pcts) for atype, pcts in aim_type_pcts.items()}
        all_pcts = [p for pcts in aim_type_pcts.values() for p in pcts]
        global_avg_pct = sum(all_pcts) / len(all_pcts) if all_pcts else 50.0
        self._aim_type_avgs = aim_type_avgs

        stats_dir = self._get_stats_dir()
        # Use cached local stats unless marked dirty
        if self._local_stats_dirty:
            self._local_stats_cache = _get_local_stats(stats_dir)
            self._local_stats_dirty = False
        local_stats = self._local_stats_cache
        now = datetime.datetime.now()
        entry_history = self._scores_cache.get("entry_history", {})

        SCENARIO_BLACKLIST = {
        }
        
        show_hidden = self._filters.get("hidden").get() if "hidden" in self._filters else False

        for lid, info in scenario_info.items():
            sname = info["name"]
            if sname in SCENARIO_BLACKLIST:
                continue
                
            is_hidden = lid in self._hidden_scenarios
            if show_hidden and not is_hidden:
                continue
            if not show_hidden and is_hidden:
                continue

            has_user = lid in user_by_lid
            has_friends = lid in friends_by_lid
            lstats = local_stats.get(sname, {"count": 0, "last_played": None, "trend": 1.0})

            hist = entry_history.get(lid, {})
            popularity_trend = 0.0
            actual_new_entries = 0
            if hist:
                dates = sorted(hist.keys())
                if len(dates) >= 2:
                    try:
                        d0_str = dates[0] if len(dates[0]) > 10 else dates[0] + "T00:00:00"
                        d0_str = d0_str.replace("Z", "+00:00") if "Z" in d0_str else d0_str
                        oldest = datetime.datetime.fromisoformat(d0_str).replace(tzinfo=None)
                        
                        d1_str = dates[-1] if len(dates[-1]) > 10 else dates[-1] + "T00:00:00"
                        d1_str = d1_str.replace("Z", "+00:00") if "Z" in d1_str else d1_str
                        newest = datetime.datetime.fromisoformat(d1_str).replace(tzinfo=None)
                        
                        seconds_diff = (newest - oldest).total_seconds()
                        if seconds_diff >= 1800:  # Need at least 30 minutes
                            # 1. Full history for stability in Trend Mult / Potential
                            days_diff = seconds_diff / 86400.0
                            entry_diff_total = hist[dates[-1]] - hist[dates[0]]
                            popularity_trend = float(entry_diff_total) / days_diff
                            
                            # 2. 24h history for "New Entries (24h)" display
                            target_24h = newest - datetime.timedelta(days=1)
                            idx_24h = 0
                            for i in range(len(dates) - 1, -1, -1):
                                ds = dates[i].replace("Z", "+00:00") if "Z" in dates[i] else dates[i]
                                if len(ds) <= 10: ds += "T00:00:00"
                                dt = datetime.datetime.fromisoformat(ds).replace(tzinfo=None)
                                if dt <= target_24h:
                                    idx_24h = i
                                    break
                            actual_new_entries = hist[dates[-1]] - hist[dates[idx_24h]]
                    except ValueError:
                        pass

            competition_multiplier = max(0.2, math.log10(max(1.0, popularity_trend + 1.0)) / 2.0)

            row = {
                "Scenario": sname,
                "Entry Count": str(info["entries"]),
                "New Entries (24h)": str(actual_new_entries) if actual_new_entries > 0 else "0",
                "Trend Mult": f"{competition_multiplier:.2f}x",
                "Local Runs": str(lstats["count"]),
                "Potential": "",
            }

            try:
                e_val = int(info["entries"])
                if has_user:
                    r_val = int(user_by_lid[lid]["rank"])
                    self._global_points_sum += (e_val - r_val)
                    self._global_potential_points_sum += (r_val - 1)
                    
                    expected_pct = aim_type_avgs.get(info.get("aimType"), global_avg_pct)
                    expected_rank = max(1, int(e_val * (1.0 - expected_pct / 100.0)))
                    if r_val > expected_rank:
                        gain = r_val - expected_rank
                        self._global_projected_gain_sum += gain
                        row["_projected_gain"] = gain
                else:
                    self._global_potential_points_sum += (e_val - 1)
                    
                    expected_pct = aim_type_avgs.get(info.get("aimType"), global_avg_pct)
                    expected_rank = max(1, int(e_val * (1.0 - expected_pct / 100.0)))
                    gain = e_val - expected_rank
                    self._global_projected_gain_sum += gain
                    row["_projected_gain"] = gain
            except (ValueError, TypeError):
                pass

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

                        # Calculate Potential Score (Optimized Algorithm)
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

                        # 6. Final Potential — *1000 converts small log floats to readable ints
                        potential = (base_potential * 1000) * time_factor * fatigue_factor * plateau_penalty * trend_factor * competition_multiplier
                        row["Potential"] = f"{int(potential)}"
                        # (Removed global summation of formula-based potential)

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

        if "Mock" not in type(self).__name__:
            if self._next_global_points is not None:
                if self._global_points_sum >= self._next_global_points:
                    self._next_global_points = None
                    self._next_rank_var.set("Next Rank: Loading...")
                    self._unplayed_needed_var.set("Unplayed Needed: Loading...")
                    if not self._fetching_next_rank:
                        self._fetching_next_rank = True
                        threading.Thread(target=self._fetch_next_rank_points, daemon=True).start()
                else:
                    self._update_next_rank_display()
            else:
                self._next_rank_var.set("Next Rank: Loading...")
                self._unplayed_needed_var.set("Unplayed Needed: Loading...")
                if not self._fetching_next_rank:
                    self._fetching_next_rank = True
                    threading.Thread(target=self._fetch_next_rank_points, daemon=True).start()

        return played, unplayed

    # -------------------------------------------------------------------
    # Thread-safe helpers
    # -------------------------------------------------------------------
    def _update_status(self, msg):
        logger.info(msg)
        self.run_in_gui_thread(self._status_var.set, msg)

    def _update_progress(self, current, total):
        target = (current / total) if total > 0 else 0
        self.run_in_gui_thread(self._set_progress_target, target)

    def _set_progress_target(self, target):
        self._target_progress = target
        if not self._animating_progress:
            self._animate_progress_loop()

    def _animate_progress_loop(self):
        self._animating_progress = True
        # Easing-out approach
        diff = self._target_progress - self._current_progress
        
        if abs(diff) < 0.0001:
            self._current_progress = self._target_progress
            self._progress_fill.place(relwidth=self._current_progress)
            self._animating_progress = False
            return

        # Smooth move towards target
        step = diff * 0.12
        if abs(step) < 0.001:
            step = 0.001 if diff > 0 else -0.001
            
        if abs(step) > abs(diff):
            self._current_progress = self._target_progress
        else:
            self._current_progress += step
            
        self._progress_fill.place(relwidth=self._current_progress)
        self.after(16, self._animate_progress_loop) # ~60fps

    def _shimmer_loop(self):
        # Hide shimmer when progress is complete (100%)
        if self._current_progress >= 0.999:
            if self._progress_shimmer.place_info():
                self._progress_shimmer.place_forget()
            self.after(200, self._shimmer_loop)
            return

        # Move shimmer from left to right
        try:
            curr_info = self._progress_shimmer.place_info()
            if not curr_info:
                # Re-show if it was hidden
                self._progress_shimmer.place(relx=-0.5, relwidth=0.4, relheight=1.0)
                curr_x = -0.5
            else:
                curr_x = float(curr_info.get('relx', -0.5))
            
            new_x = curr_x + 0.012
            if new_x > 1.2:
                new_x = -0.5
            self._progress_shimmer.place(relx=new_x)
        except Exception:
            pass
        self.after(25, self._shimmer_loop)

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

    def _on_right_click(self, event):
        """Handle right-click on the treeview to show context menus."""
        region = self._tree.identify_region(event.x, event.y)
        if region == "heading":
            self._show_column_menu(event)
        elif region == "cell":
            self._show_row_menu(event)

    def _show_row_menu(self, event):
        """Show context menu for a specific row."""
        row_id = self._tree.identify_row(event.y)
        if not row_id:
            return
            
        self._tree.selection_set(row_id)
        values = self._tree.item(row_id, "values")
        if not values:
            return
            
        sname = values[1]
        lid = None
        for k, v in self._scenario_info.items():
            if v["name"] == sname:
                lid = k
                break
                
        if not lid:
            return
            
        menu = tk.Menu(self, tearoff=0, bg=BG_LIGHTER, fg=TEXT,
                       activebackground=ACCENT, activeforeground="#fff",
                       font=("Segoe UI", 9), bd=1, relief="solid")
                       
        menu.add_command(label="📋 Copy Scenario Name", command=lambda: self._copy_scenario_name(event))
        
        if lid in self._hidden_scenarios:
            menu.add_command(label="👁️ Unhide Scenario", command=lambda: self._toggle_hide_scenario(lid))
        else:
            menu.add_command(label="👁️ Hide Scenario", command=lambda: self._toggle_hide_scenario(lid))
            
        menu.tk_popup(event.x_root, event.y_root)

    def _toggle_hide_scenario(self, lid):
        """Toggle the hidden status of a scenario."""
        if lid in self._hidden_scenarios:
            self._hidden_scenarios.remove(lid)
            self._update_status("Scenario unhidden.")
        else:
            self._hidden_scenarios.add(lid)
            self._update_status("Scenario hidden.")
            
        self._cfg["hidden_scenarios"] = list(self._hidden_scenarios)
        save_config(self._cfg)
        self._rebuild_data()

    def _show_column_menu(self, event):
        """Show a context menu to toggle column visibility."""
        auto_hidden = self._get_auto_hidden_cols()
        menu = tk.Menu(self, tearoff=0, bg=BG_LIGHTER, fg=TEXT,
                       activebackground=ACCENT, activeforeground="#fff",
                       font=("Segoe UI", 9),
                       disabledforeground=TEXT_DIM,
                       selectcolor="white")
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

    def run_in_gui_thread(self, func, *args, **kwargs):
        """Put a callback onto the GUI queue to be executed on the main thread."""
        self._gui_queue.put(lambda: func(*args, **kwargs))

    def _process_gui_queue(self):
        """Process tasks from the thread-safe GUI queue on the main thread."""
        for _ in range(100):
            try:
                task = self._gui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                task()
            except Exception as e:
                logger.exception("Error executing queued GUI action: %s", e)
        self.after(20, self._process_gui_queue)

    # -------------------------------------------------------------------
    # Log panel
    # -------------------------------------------------------------------
    def _setup_log_redirect(self):
        sys.stdout = StdoutRedirector(self._append_log, sys.__stdout__)
        sys.stderr = StdoutRedirector(self._append_log, sys.__stderr__)

        gui_handler = logging.Handler()
        gui_handler.setLevel(logger.level)
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
        self.run_in_gui_thread(_do)

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
                self._local_stats_dirty = True
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

        # Autoplay: advance immediately on local score detection (before API calls).
        # We do this BEFORE the UI refresh so we can find the "next" item in the current sorted view
        # before the finished scenario potentially jumps to a new position.
        if self._autoplay_var.get() and self._autoplay_current_scenario:
            if snames & {self._autoplay_current_scenario}:
                logger.info("Autoplay: local score detected for '%s', advancing…",
                            self._autoplay_current_scenario)
                self.run_in_gui_thread(self._autoplay_advance)

        # Trigger UI refresh immediately to show new "Local Runs" counts.
        # This is queued after autoplay advance so the next scenario is already selected.
        self.run_in_gui_thread(self._rebuild_data)

        if not lids_to_update:
            return
            
        # Give client a few seconds to upload the stats first
        time.sleep(3)
            
        username = self._cfg.get("username", "").strip()
        password = self._password
            
        if not self._jwt_token:
            if not username or not password:
                logger.info("Auto-sync: Local run detected, but cannot fetch API scores (not logged in).")
                # Still rebuild to show the local run increment
                self.run_in_gui_thread(self._rebuild_data)
                return
            try:
                self._jwt_token = kovaaks_login(username, password)
            except Exception as e:
                logger.debug("Failed silent login during stats poll: %s", e)
                self.run_in_gui_thread(self._rebuild_data)
                return
                
        updated = False
        session = requests.Session()
        for lid, expected_score in lids_to_update.items():
            max_attempts = 5
            for attempt in range(max_attempts):
                try:
                    data = kovaaks_get_friends_scores(self._jwt_token, lid, session=session)
                    
                    username = self._cfg.get("username", "").strip()
                    user_entry, friend_entries = parse_leaderboard_entries(data, username)
                    
                    if not user_entry and data:
                        all_names = [entry.get("webappUsername") or entry.get("steamAccountName", "") for entry in data]
                        logger.debug("Auto-sync: User '%s' not found in leaderboard data for lid=%s. Candidates: %s", 
                                     username, lid, all_names[:5])
                    
                    target_met = True
                    if user_entry and expected_score > -999999.0:
                        try:
                            # Clean up the score string the same way display sorting does
                            clean_score = str(user_entry["score"]).replace(",", "")
                            if clean_score.endswith("%"):
                                clean_score = clean_score[:-1]
                            api_score = float(clean_score)
                            
                            # Use a small epsilon to avoid precision issues
                            if api_score < expected_score - 0.001:
                                target_met = False
                        except (ValueError, TypeError):
                            pass
                            
                    if target_met or attempt == max_attempts - 1:
                        if user_entry:
                            self._user_by_lid[lid] = user_entry
                            updated = True
                            sname = self._scenario_info.get(lid, {}).get("name", lid)
                            logger.info("Auto-updated score for %s", sname)
                        if friend_entries:
                            self._friends_by_lid[lid] = friend_entries
                            
                        # Update cache structure
                        if lid not in self._scores_cache.setdefault("scores", {}):
                            self._scores_cache["scores"][lid] = {}
                        if user_entry:
                            self._scores_cache["scores"][lid]["user"] = user_entry
                        if friend_entries:
                            self._scores_cache["scores"][lid]["friends"] = friend_entries
                            
                        break
                    else:
                        logger.debug("Score for lid=%s not updated yet in API, retrying (%d/%d)...", lid, attempt+1, max_attempts)
                        time.sleep(4)
                except Exception as e:
                    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None and e.response.status_code == 401:
                        logger.warning("Session expired during auto-update. Attempting re-login.")
                        self._jwt_token = None
                        if username and password:
                            try:
                                self._jwt_token = kovaaks_login(username, password)
                                # Token refreshed, try the same attempt again immediately
                                continue 
                            except Exception as le:
                                logger.debug("Re-login failed during auto-update: %s", le)
                    
                    logger.debug("Failed auto-update for lid=%s on attempt %d/%d: %s", lid, attempt+1, max_attempts, e)
                    time.sleep(4)
                
        if updated:
            save_scores_cache(self._scores_cache)
        
        # Always rebuild at the end to ensure UI is in sync even if API fetch failed
        self.run_in_gui_thread(self._rebuild_data)

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
            self._autoplay_btn.configure(bg=ACCENT, fg="#fff")
            self._autoplay_btn.bind("<Enter>",
                lambda e: self._autoplay_btn.configure(bg=ACCENT_HOVER))
            self._autoplay_btn.bind("<Leave>",
                lambda e: self._autoplay_btn.configure(bg=ACCENT))
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
            self._autoplay_btn.configure(bg=ACCENT, fg="#fff")
            self._autoplay_btn.bind("<Enter>",
                lambda e: self._autoplay_btn.configure(bg=ACCENT_HOVER))
            self._autoplay_btn.bind("<Leave>",
                lambda e: self._autoplay_btn.configure(bg=ACCENT))
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


    def _record_history_points(self, scenarios_list):
        """Record current entry counts into the local history cache."""
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        now_str = now.isoformat()
        history = self._scores_cache.get("entry_history", {})
        
        # Clean up any "future" timestamps caused by previous timezone mismatches
        for lid in list(history.keys()):
            bad_keys = []
            for k in history[lid].keys():
                try:
                    lk = k.replace("Z", "+00:00") if "Z" in k else k
                    dt = datetime.datetime.fromisoformat(lk).replace(tzinfo=None)
                    if (dt - now).total_seconds() > 3600:
                        bad_keys.append(k)
                except ValueError:
                    pass
            for k in bad_keys:
                del history[lid][k]
        
        for s in scenarios_list:
            lid = str(s.get("leaderboardId", ""))
            entries = s.get("counts", {}).get("entries", 0)
            try:
                entries = int(entries)
            except (ValueError, TypeError):
                continue
                
            if lid not in history:
                history[lid] = {}
                
            if history[lid]:
                latest_key = max(history[lid].keys())
                try:
                    lk = latest_key.replace("Z", "+00:00") if "Z" in latest_key else latest_key
                    latest_dt = datetime.datetime.fromisoformat(lk).replace(tzinfo=None)
                    if (now - latest_dt).total_seconds() < 3600:
                        # Update the value of the existing latest timestamp instead of making a new one
                        history[lid][latest_key] = entries
                        continue
                except ValueError:
                    pass
                    
            history[lid][now_str] = entries
            # Prune to last 168 records (7 days)
            while len(history[lid]) > 168:
                oldest_key = min(history[lid].keys())
                del history[lid][oldest_key]
        self._scores_cache["entry_history"] = history


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = KovaaksApp()
    app.mainloop()
