#!/usr/bin/env python3
import os
import sys

# Disable WebKitGTK DMABuf renderer to fix scrolling lag/flicker on Linux
if sys.platform.startswith('linux'):
    os.environ["WEBKIT_DISABLE_DMABUF_RENDERER"] = "1"

import webview
import json
import logging
import threading
import time
import datetime
import math
from pathlib import Path

from kovaaks.constants import MIN_ENTRIES
from kovaaks.config_helpers import load_config
from kovaaks.cache import load_scores_cache, load_scenarios_from_cache
from kovaaks.stats import get_local_stats as _get_local_stats
from kovaaks.fetch_worker import run_fetch_all
from kovaaks.data_processing import safe_int

from kovaaks.logging_helpers import setup_logging

logger = setup_logging()

class KovaaksAPI:
    def __init__(self):
        self.window = None
        self._cfg = load_config()
        self._scores_cache = {}
        self._scenario_info = {}
        self._user_by_lid = {}
        self._friends_by_lid = {}
        self._local_stats_dirty = True
        self._local_stats_cache = {}
        self._hidden_scenarios = set(self._cfg.get("hidden_scenarios", []))
        self._filters = {}
        self._known_stat_files = set()
        self._jwt_token = None
        
        self._load_cache_and_populate()

    def set_window(self, window):
        self.window = window
        self._start_stats_polling()

    def _get_stats_dir(self):
        return self._cfg.get("stats_dir", "")
        
    def _update_status(self, msg):
        logger.info(msg)
        if self.window:
            import json
            safe_msg = json.dumps(msg)
            self.window.evaluate_js(f"if(window.setStatus) window.setStatus({safe_msg})")
            
    def _update_progress(self, current, total):
        if self.window:
            self.window.evaluate_js(f"if(window.updateProgress) window.updateProgress({current}, {total})")

    def _load_cache_and_populate(self):
        """Load the unified JSON cache and populate tabs with cached data."""
        if not self._scores_cache:
            self._scores_cache = load_scores_cache()

        self._zombies = set(self._scores_cache.setdefault("zombies", []))
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
            f"Rebuilt from memory cache — {len(played)} played, {len(unplayed)} unplayed"
        )
        self._update_progress(1, 1)

    # -------------------------------------------------------------------
    # Settings
    # -------------------------------------------------------------------


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

            import re
            norm_name = re.sub(r'[^a-z0-9]', '', sname.lower())
            if hasattr(self, "_zombies") and norm_name in self._zombies:
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

            import re
            norm_name = re.sub(r'[^a-z0-9]', '', sname.lower())
            is_zombie = hasattr(self, "_zombies") and norm_name in self._zombies

            row = {
                "Scenario": sname,
                "Entry Count": str(info["entries"]),
                "New Entries (24h)": str(actual_new_entries) if actual_new_entries > 0 else "0",
                "Trend Mult": f"{competition_multiplier:.2f}x",
                "Local Runs": str(lstats["count"]),
                "Potential": "",
                "_is_zombie": is_zombie,
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
                row["Top Friend"] = best[0] if best else ""
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
                row["Top Friend"] = ""
                row["Friend Rank"] = ""
                row["Friend Score"] = ""
                row["Friend Percentile"] = ""
                row["Friend Score Date"] = ""
                row["Rank Diff"] = ""
                row["Pctile Diff"] = ""

            rows.append(row)

        played_rows = []
        unplayed_rows = []
        for r in rows:
            if r.get("My Rank") or r.get("Top Friend"):
                played_rows.append(r)
            else:
                unplayed_rows.append(r)
        return played_rows, unplayed_rows

    # -------------------------------------------------------------------
    # Thread-safe helpers
    # -------------------------------------------------------------------



    def run_in_gui_thread(self, func):
        func()
        
    def _set_running(self, running):
        pass
        
    def _rebuild_data_and_finish(self, errors=0):
        self._update_status(f"Fetch complete with {errors} errors.")
        self._update_progress(0, 0)
        if self.window:
            self.window.evaluate_js("fetchData()")

    def _record_history_points(self, scenarios_list):
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        now_str = now.isoformat()
        history = self._scores_cache.get("entry_history", {})

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
                        history[lid][latest_key] = entries
                        continue
                except ValueError:
                    pass
            history[lid][now_str] = entries
            while len(history[lid]) > 168:
                oldest_key = min(history[lid].keys())
                del history[lid][oldest_key]
        self._scores_cache["entry_history"] = history

    def get_data(self, min_entries, show_hidden=False):
        self._cfg["min_entries"] = min_entries
        class DummyVar:
            def __init__(self, val): self.val = val
            def get(self): return self.val
        self._filters["hidden"] = DummyVar(show_hidden)
        self._load_cache_and_populate()
        try:
            played, unplayed = self._rebuild_data()
            all_data = played + unplayed
            
            if not all_data:
                return {"columns": [], "rows": [], "global_stats": {}}
                
            cols = list(all_data[0].keys())
            # filter out private keys
            cols = [c for c in cols if not c.startswith("_")]
            
            rows = []
            zombies_list = []
            for d in all_data:
                rows.append([d.get(c, "") for c in cols])
                if d.get("_is_zombie"):
                    zombies_list.append(d.get("Scenario"))
                
            global_stats = {
                "points": getattr(self, '_global_points_sum', 0),
                "potential_points": getattr(self, '_global_potential_points_sum', 0),
                "projected_gain": getattr(self, '_global_projected_gain_sum', 0),
                "total_rows": len(all_data)
            }
                
            return {
                "columns": cols,
                "rows": rows,
                "global_stats": global_stats,
                "zombies": zombies_list
            }
        except Exception as e:
            logger.exception("Error in get_data")
            return {"columns": [], "rows": [], "global_stats": {}}

    def get_next_rank_points(self):
        try:
            current_points = getattr(self, '_global_points_sum', 0)
            if current_points <= 0:
                return "N/A"
            username = self._cfg.get("username", "").strip()
            if not username:
                return "N/A (No Username)"
            
            import kovaaks.api as api
            next_points = api.get_next_leaderboard_position_points(username, current_points)
            if next_points and next_points > current_points:
                diff = int(next_points - current_points)
                return f"+{diff:,}"
            else:
                return "Rank 1!"
        except Exception as e:
            logger.warning("Error fetching next rank points: %s", e)
            return "Error"

    def get_logs(self):
        log_file = "kovaaks.log"
        if os.path.exists(log_file):
            size = os.path.getsize(log_file)
            chunk_size = 64 * 1024 # 64 KB
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                if size > chunk_size:
                    f.seek(size - chunk_size)
                    f.readline() # drop first partial line
                lines = f.readlines()
                return "".join(lines[-1000:])
        return "No logs found."

    def clear_logs(self):
        log_file = "kovaaks.log"
        if os.path.exists(log_file):
            open(log_file, "w").close()
            return True
        return False

    def toggle_hide_scenario(self, scenario_name):
        from kovaaks.config_helpers import save_config
        lid = None
        for k, v in self._scenario_info.items():
            if v["name"] == scenario_name:
                lid = k
                break
                
        if not lid:
            return False

        if lid in self._hidden_scenarios:
            self._hidden_scenarios.remove(lid)
        else:
            self._hidden_scenarios.add(lid)
            
        self._cfg["hidden_scenarios"] = list(self._hidden_scenarios)
        save_config(self._cfg)
        return True

    def get_config(self):
        from kovaaks.config_helpers import get_default_stats_dir
        return {
            "username": self._cfg.get("username", ""),
            "password": self._cfg.get("password", ""),
            "has_password": bool(self._cfg.get("password", "")),
            "stats_dir": self._cfg.get("stats_dir", get_default_stats_dir()),
            "min_entries": self._cfg.get("min_entries", 1000),
            "auto_refresh": self._cfg.get("auto_refresh", False),
            "auto_refresh_github_only": self._cfg.get("auto_refresh_github_only", False),
            "refresh_interval": self._cfg.get("refresh_interval", 60),
            "always_show_total_points": self._cfg.get("always_show_total_points", True),
            "auto_fit_columns": self._cfg.get("auto_fit_columns", False),
            "visible_columns": self._cfg.get("visible_columns", None),
            "column_widths": self._cfg.get("column_widths", {})
        }

    def save_settings(self, settings):
        from kovaaks.config_helpers import save_config
        old_stats_dir = self._cfg.get("stats_dir")
        self._cfg.update(settings)
        save_config(self._cfg)
        
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
            self._scores_cache["known_stat_files"] = list(self._known_stat_files)
            from kovaaks.cache import save_scores_cache
            save_scores_cache(self._scores_cache)
            self._local_stats_dirty = True

    def save_credentials(self, username, password):
        from kovaaks.config_helpers import save_config
        self._cfg["username"] = username
        self._cfg["password"] = password
        save_config(self._cfg)

    def get_clipboard(self):
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            text = root.clipboard_get()
            root.destroy()
            return text
        except Exception as e:
            logger.warning("Could not read clipboard from python: %s", e)
            return ""

    def fetch_all_stats(self):
        username = self._cfg.get("username", "")
        password = self._cfg.get("password", "")
        threading.Thread(target=run_fetch_all, args=(self, username, password), daemon=True).start()

    def play_scenario(self, name):
        import urllib.parse
        import webbrowser
        from kovaaks.constants import STEAM_LAUNCH_URI
        from kovaaks.api import is_scenario_zombie
        from kovaaks.cache import save_scores_cache

        if not hasattr(self, "_zombies"):
            self._zombies = set(self._scores_cache.setdefault("zombies", []))

        stats_dir = self._get_stats_dir()

        def check_and_launch():
            self._update_status(f"Checking scenario availability: {name}...")
            try:
                is_zombie = is_scenario_zombie(name, stats_dir, self._zombies)
            except Exception as e:
                logger.error("Error in background zombie check for '%s': %s", name, e)
                is_zombie = False

            if is_zombie:
                import re
                norm_name = re.sub(r'[^a-z0-9]', '', name.lower())
                if norm_name not in self._zombies:
                    self._zombies.add(norm_name)
                    self._scores_cache["zombies"] = list(self._zombies)
                    save_scores_cache(self._scores_cache)

                self._update_status(f"Error: '{name}' has been deleted from Steam Workshop.")
                logger.warning("Scenario '%s' is a zombie scenario (deleted from Steam Workshop).", name)

                if self.window:
                    self.window.evaluate_js("if(window.fetchData) window.fetchData()")
            else:
                try:
                    uri = STEAM_LAUNCH_URI.format(urllib.parse.quote(name, safe=""))
                    self._update_status(f"Launching: {name}")
                    webbrowser.open(uri)
                except Exception as e:
                    logger.exception("Error launching scenario: %s", name)
                    self._update_status(f"Error launching: {name}")

        threading.Thread(target=check_and_launch, daemon=True).start()
        return True

    def update_status(self, msg):
        self._update_status(msg)

    def _start_stats_polling(self):
        stats_dir = self._get_stats_dir()
        if os.path.exists(stats_dir):
            try:
                current_files = set(f for f in os.listdir(stats_dir) if f.endswith(" Stats.csv"))
                if "known_stat_files" in self._scores_cache:
                    cached_known = set(self._scores_cache["known_stat_files"])
                    new_files = current_files - cached_known
                else:
                    new_files = set()
                
                self._known_stat_files = current_files
                self._scores_cache["known_stat_files"] = list(self._known_stat_files)
                from kovaaks.cache import save_scores_cache
                save_scores_cache(self._scores_cache)
                
                if new_files:
                    self._local_stats_dirty = True
                    threading.Thread(
                        target=self._handle_new_stats_files,
                        args=(stats_dir, new_files),
                        daemon=True
                    ).start()
            except OSError:
                pass
        
        threading.Thread(target=self._poll_stats_loop, daemon=True).start()

    def _poll_stats_loop(self):
        while True:
            time.sleep(5)
            stats_dir = self._get_stats_dir()
            if not stats_dir or not os.path.exists(stats_dir):
                continue
            try:
                current_files = set(f for f in os.listdir(stats_dir) if f.endswith(" Stats.csv"))
                new_files = current_files - self._known_stat_files
                if new_files:
                    self._known_stat_files.update(new_files)
                    self._scores_cache["known_stat_files"] = list(self._known_stat_files)
                    from kovaaks.cache import save_scores_cache
                    save_scores_cache(self._scores_cache)
                    
                    self._local_stats_dirty = True
                    self._handle_new_stats_files(stats_dir, new_files)
            except OSError:
                pass

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

        # Notify JS to reload the table data (to show the new local runs count)
        if self.window:
            self.window.evaluate_js("if(window.fetchData) window.fetchData()")

        # Notify JS of the new local runs
        for sname in snames:
            if self.window:
                import json
                safe_sname = json.dumps(sname)
                self.window.evaluate_js(f"if (window.onLocalScoreDetected) window.onLocalScoreDetected({safe_sname})")

        if not lids_to_update:
            return

        # Give client a few seconds to upload the stats first
        time.sleep(3)

        username = self._cfg.get("username", "").strip()
        password = self._cfg.get("password", "")

        if not getattr(self, "_jwt_token", None):
            if not username or not password:
                logger.info("Auto-sync: Local run detected, but cannot fetch API scores (not logged in).")
                return
            try:
                from kovaaks.api import kovaaks_login
                self._jwt_token = kovaaks_login(username, password)
            except Exception as e:
                logger.debug("Failed silent login during stats poll: %s", e)
                return

        updated = False
        import requests
        from kovaaks.api import kovaaks_get_friends_scores
        from kovaaks.data_processing import parse_leaderboard_entries
        from kovaaks.cache import save_scores_cache

        session = requests.Session()
        for lid, expected_score in lids_to_update.items():
            max_attempts = 5
            for attempt in range(max_attempts):
                try:
                    data = kovaaks_get_friends_scores(self._jwt_token, lid, session=session)
                    
                    user_entry, friend_entries = parse_leaderboard_entries(data, username)
                    
                    target_met = True
                    if user_entry and expected_score > -999999.0:
                        try:
                            clean_score = str(user_entry["score"]).replace(",", "")
                            if clean_score.endswith("%"):
                                clean_score = clean_score[:-1]
                            api_score = float(clean_score)
                            
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
                                from kovaaks.api import kovaaks_login
                                self._jwt_token = kovaaks_login(username, password)
                                continue
                            except Exception as le:
                                logger.debug("Re-login failed during auto-update: %s", le)
                    
                    logger.debug("Failed auto-update for lid=%s on attempt %d/%d: %s", lid, attempt+1, max_attempts, e)
                    time.sleep(4)
                
        if updated:
            save_scores_cache(self._scores_cache)
            # Notify JS to reload the table data (to show the new API scores)
            if self.window:
                self.window.evaluate_js("if(window.fetchData) window.fetchData()")




if __name__ == "__main__":
    api = KovaaksAPI()
    window = webview.create_window(
        "KovaaK's Scenario Tracker",
        "web/index.html",
        js_api=api,
        width=1200,
        height=800,
        min_size=(800, 600),
        background_color="#121212"
    )
    api.set_window(window)
    # Force GTK backend on Linux by default (QtWebKit is deprecated and crashes on modern Arch),
    # but allow overriding it via '--gui <backend>' (e.g., '--gui qt') if modern QtWebEngine is installed.
    gui_backend = 'gtk' if sys.platform.startswith('linux') else None
    if "--gui" in sys.argv:
        try:
            idx = sys.argv.index("--gui")
            gui_backend = sys.argv[idx + 1]
        except (ValueError, IndexError):
            pass
    webview.start(gui=gui_backend, debug=False)
