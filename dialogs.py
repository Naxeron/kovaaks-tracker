import tkinter as tk
from tkinter import ttk

from constants import (
    BG, BG_LIGHTER, ENTRY_BG, TEXT, TEXT_DIM, ACCENT, ACCENT_HOVER, BORDER
)
from config_helpers import get_default_stats_dir
from data_processing import get_estimated_matching_count

class PasswordDialog(tk.Toplevel):
    """Modal dialog for KovaaKs password prompt on startup."""

    def __init__(self, parent, username):
        super().__init__(parent)
        self.title("KovaaKs Login")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result = None

        pad = {"padx": 20, "pady": 10}
        
        ttk.Label(self, text=f"Logging in as: {username}", style="Dark.TLabel").pack(**pad)
        
        frame = tk.Frame(self, bg=BG)
        frame.pack(fill="x", padx=20)
        
        ttk.Label(frame, text="Password:", style="Dark.TLabel").pack(side="left")
        self._entry_var = tk.StringVar()
        
        # Container for entry + toggle button
        pw_container = tk.Frame(frame, bg=ENTRY_BG)
        pw_container.pack(side="left", padx=10)

        self._entry = tk.Entry(pw_container, textvariable=self._entry_var, width=26,
                               bg=ENTRY_BG, fg=TEXT, insertbackground=TEXT,
                               font=("Segoe UI", 11), relief="flat", bd=4,
                               show="*")
        self._entry.pack(side="left")
        
        self._show_password = False
        self._toggle_btn = tk.Button(pw_container, text="\U0001F512", command=self._toggle_password,
                                   bg=ENTRY_BG, fg=TEXT_DIM, activebackground=ENTRY_BG,
                                   activeforeground=TEXT, font=("Segoe UI", 12),
                                   relief="flat", bd=0, cursor="hand2")
        self._toggle_btn.pack(side="left", padx=4)
        
        self._entry.focus_set()
        
        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.pack(pady=20)

        tk.Button(btn_frame, text="  Login  ", command=self._on_login,
                  bg=ACCENT, fg="#fff", activebackground=ACCENT_HOVER,
                  activeforeground="#fff", font=("Segoe UI", 10, "bold"),
                  relief="flat", bd=0, padx=14, pady=6, cursor="hand2").pack(side="left", padx=8)

        tk.Button(btn_frame, text="  Cancel  ", command=self.destroy,
                  bg=BG_LIGHTER, fg=TEXT, activebackground=BORDER,
                  activeforeground=TEXT, font=("Segoe UI", 10),
                  relief="flat", bd=0, padx=14, pady=6, cursor="hand2").pack(side="left", padx=8)

        self.bind("<Return>", lambda e: self._on_login())
        self.bind("<Escape>", lambda e: self.destroy())

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _toggle_password(self):
        self._show_password = not self._show_password
        if self._show_password:
            self._entry.config(show="")
            self._toggle_btn.config(text="\U0001F441", bg=ACCENT, fg="#fff", 
                                    activebackground=ACCENT_HOVER)
        else:
            self._entry.config(show="*")
            self._toggle_btn.config(text="\U0001F512", bg=ENTRY_BG, fg=TEXT_DIM, 
                                    activebackground=ENTRY_BG)

    def _on_login(self):
        self.result = self._entry_var.get().strip()
        self.destroy()

class SettingsDialog(tk.Toplevel):
    """Modal dialog for KovaaKs username and other settings."""

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
            ("KovaaKs Password (Session Only)", "password", cfg.get("password", "")),
            ("Stats Folder", "stats_dir", cfg.get("stats_dir", get_default_stats_dir())),
            ("Min Entries for Fetching", "min_entries", cfg.get("min_entries", "1000")),
        ]
        self._entries = {}

        for label_text, key, default in fields:
            ttk.Label(self, text=label_text, style="Dark.TLabel").grid(
                row=row, column=0, sticky="w", **pad)
            var = tk.StringVar(value=default)
            show_char = "*" if key == "password" else ""
            self._entries[key] = var
            
            if key == "min_entries":
                frame = tk.Frame(self, bg=BG)
                frame.grid(row=row, column=1, sticky="w", **pad)
                entry = tk.Entry(frame, textvariable=var, width=15,
                                 bg=ENTRY_BG, fg=TEXT, insertbackground=TEXT,
                                 font=("Segoe UI", 11), relief="flat", bd=4,
                                 show=show_char)
                entry.pack(side="left")
                
                self._est_label = ttk.Label(frame, text="", style="Dark.TLabel", foreground=TEXT_DIM)
                self._est_label.pack(side="left", padx=8)
                
                var.trace_add("write", self._update_estimate)
            elif key == "password":
                pw_container = tk.Frame(self, bg=ENTRY_BG)
                pw_container.grid(row=row, column=1, sticky="w", **pad)
                
                entry = tk.Entry(pw_container, textvariable=var, width=38,
                                 bg=ENTRY_BG, fg=TEXT, insertbackground=TEXT,
                                 font=("Segoe UI", 11), relief="flat", bd=4,
                                 show=show_char)
                entry.pack(side="left")
                
                # We need to store a reference to the entry to toggle it
                self._password_entry = entry
                self._password_show = False
                
                self._password_toggle_btn = tk.Button(pw_container, text="\U0001F512", 
                                           command=self._toggle_password_visibility,
                                           bg=ENTRY_BG, fg=TEXT_DIM, activebackground=ENTRY_BG,
                                           activeforeground=TEXT, font=("Segoe UI", 12),
                                           relief="flat", bd=0, cursor="hand2")
                self._password_toggle_btn.pack(side="left", padx=4)
            else:
                entry = tk.Entry(self, textvariable=var, width=42,
                                 bg=ENTRY_BG, fg=TEXT, insertbackground=TEXT,
                                 font=("Segoe UI", 11), relief="flat", bd=4,
                                 show=show_char)
                entry.grid(row=row, column=1, **pad)
                
            row += 1
            
        # Auto Refresh Toggle
        ttk.Label(self, text="Auto Refresh", style="Dark.TLabel").grid(
            row=row, column=0, sticky="w", **pad)
        self._auto_refresh_var = tk.BooleanVar(value=cfg.get("auto_refresh", False))
        self._auto_refresh_cb = tk.Checkbutton(self, variable=self._auto_refresh_var, bg=BG, activebackground=BG,
                            selectcolor=BG, bd=0, highlightthickness=0,
                            fg="white", activeforeground="white")
        self._auto_refresh_cb.grid(row=row, column=1, sticky="w", padx=12)
        row += 1

        # Wait for scenario data update
        self._github_label = ttk.Label(self, text="Wait for scenario data update", style="Dark.TLabel")
        self._github_label.grid(row=row, column=0, sticky="w", **pad)
        self._auto_refresh_github_var = tk.BooleanVar(value=cfg.get("auto_refresh_github_only", False))
        self._auto_refresh_github_cb = tk.Checkbutton(self, variable=self._auto_refresh_github_var, bg=BG, activebackground=BG,
                             selectcolor=BG, bd=0, highlightthickness=0,
                             fg="white", activeforeground="white")
        self._auto_refresh_github_cb.grid(row=row, column=1, sticky="w", padx=12)
        row += 1

        # Refresh Interval
        self._interval_label = ttk.Label(self, text="Refresh Interval (min)", style="Dark.TLabel")
        self._interval_label.grid(row=row, column=0, sticky="w", **pad)
        self._refresh_interval_var = tk.StringVar(value=cfg.get("refresh_interval", "60"))
        self._refresh_interval_entry = tk.Entry(self, textvariable=self._refresh_interval_var, width=15,
                         bg=ENTRY_BG, fg=TEXT, insertbackground=TEXT,
                         font=("Segoe UI", 11), relief="flat", bd=4)
        self._refresh_interval_entry.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        # Always show total points
        ttk.Label(self, text="Always display total points", style="Dark.TLabel").grid(
            row=row, column=0, sticky="w", **pad)
        self._always_show_total_points_var = tk.BooleanVar(value=cfg.get("always_show_total_points", True))
        self._always_show_total_points_cb = tk.Checkbutton(self, variable=self._always_show_total_points_var, bg=BG, activebackground=BG,
                                selectcolor=BG, bd=0, highlightthickness=0,
                                fg="white", activeforeground="white")
        self._always_show_total_points_cb.grid(row=row, column=1, sticky="w", padx=12)
        row += 1

        # Traces for UI interactivity
        self._auto_refresh_var.trace_add("write", self._update_ui_states)
        self._auto_refresh_github_var.trace_add("write", self._update_ui_states)
        self._update_ui_states()

        if "min_entries" in self._entries:
            self._update_estimate()

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

    def _update_ui_states(self, *_args):
        refresh_on = self._auto_refresh_var.get()
        github_only = self._auto_refresh_github_var.get()
        
        # GitHub only toggle is only relevant if auto refresh is ON
        if refresh_on:
            self._auto_refresh_github_cb.config(state="normal")
            self._github_label.config(foreground=TEXT)
        else:
            self._auto_refresh_github_cb.config(state="disabled")
            self._github_label.config(foreground=TEXT_DIM)
            
        # Interval entry is only relevant if auto refresh is ON AND github only is OFF
        if refresh_on and not github_only:
            self._refresh_interval_entry.config(state="normal")
            self._interval_label.config(foreground=TEXT)
        else:
            self._refresh_interval_entry.config(state="disabled")
            self._interval_label.config(foreground=TEXT_DIM)

    def _on_save(self):
        self.result = {k: v.get().strip() for k, v in self._entries.items()}
        self.result["auto_refresh"] = self._auto_refresh_var.get()
        self.result["auto_refresh_github_only"] = self._auto_refresh_github_var.get()
        self.result["refresh_interval"] = self._refresh_interval_var.get().strip()
        self.result["always_show_total_points"] = self._always_show_total_points_var.get()
        self.destroy()

    def _toggle_password_visibility(self):
        self._password_show = not self._password_show
        if self._password_show:
            self._password_entry.config(show="")
            self._password_toggle_btn.config(text="\U0001F441", bg=ACCENT, fg="#fff",
                                             activebackground=ACCENT_HOVER)
        else:
            self._password_entry.config(show="*")
            self._password_toggle_btn.config(text="\U0001F512", bg=ENTRY_BG, fg=TEXT_DIM,
                                             activebackground=ENTRY_BG)

    def _update_estimate(self, *_args):
        if not hasattr(self, "_est_label"):
            return
        val = self._entries["min_entries"].get().strip()
        try:
            m = int(val)
        except ValueError:
            self._est_label.config(text="")
            return
        
        n_scenarios = get_estimated_matching_count(m)
        
        # ~0.02s total per valid scenario (score fetch only)
        total_seconds = int(n_scenarios * 0.02)
        if total_seconds < 60:
            est_text = f"~{total_seconds}s ETA"
        else:
            mins = total_seconds // 60
            secs = total_seconds % 60
            est_text = f"~{mins}m {secs}s ETA"
        self._est_label.config(text=est_text)
