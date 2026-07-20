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

from kovaaks.constants import MIN_ENTRIES
from kovaaks.config_helpers import load_config
from kovaaks.cache import load_scores_cache, load_scenarios_from_cache, save_scores_cache, SCORES_CACHE
from kovaaks.scoring import calculate_potential_score
from kovaaks.stats import get_local_stats as _get_local_stats
from kovaaks.fetch_worker import run_fetch_all
from kovaaks.data_processing import safe_int

from kovaaks.logging_helpers import setup_logging

logger = setup_logging()

def _parse_iso_dt(s):
    ds = s.replace("Z", "+00:00")
    if len(ds) <= 10:
        ds += "T00:00:00"
    return datetime.datetime.fromisoformat(ds).replace(tzinfo=None)

def _clean_aim_type(raw_type, scenario_name):
    if not raw_type:
        raw_type = ""
    raw_lower = raw_type.lower().strip()
    name_lower = str(scenario_name).lower()
    
    if "tracking" in raw_lower or "strafe" in raw_lower:
        return "Tracking"
    if "clicking" in raw_lower or "flick" in raw_lower or "static" in raw_lower:
        return "Clicking"
    if "switching" in raw_lower or "ts" in raw_lower:
        return "Target Switching"
        
    if "tracking" in name_lower or "strafe" in name_lower or "lg " in name_lower or "smooth" in name_lower or "centered" in name_lower or "centering" in name_lower or "shaft" in name_lower or "reactive" in name_lower:
        return "Tracking"
    if "click" in name_lower or "static" in name_lower or "flick" in name_lower or "popcorn" in name_lower or "pokeball" in name_lower or "1wall" in name_lower or "tile frenzy" in name_lower or "microshot" in name_lower or "pasu" in name_lower or "reflex" in name_lower:
        return "Clicking"
    if "switching" in name_lower or " ts" in name_lower or "target switch" in name_lower or "ts " in name_lower:
        return "Target Switching"
        
    if "other" in raw_lower:
        return "Other"
    return "Other / Unknown"

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
        self._scenarios_expected_gains = []
        
        self._cache_loaded_event = threading.Event()
        if "pytest" in sys.modules:
            self._load_cache_and_populate()
            # Perform initial rebuild in tests to keep behavior synchronous
            played, unplayed = self._rebuild_data()
            self._cache_loaded_event.set()
        else:
            threading.Thread(target=self._initial_cache_load, daemon=True).start()

    def _initial_cache_load(self):
        logger.info("Starting background cache load...")
        t0 = time.time()
        self._load_cache_and_populate()
        
        # Build the data once and update status/progress
        played, unplayed = self._rebuild_data()
        self._update_status(
            f"Rebuilt from memory cache — {len(played)} played, {len(unplayed)} unplayed"
        )
        self._update_progress(1, 1)
        
        # Save the updated scores cache in case get_local_stats added new local runs
        if self._scores_cache.pop("_dirty", False) and not getattr(self, "_cache_corrupted", False):
            save_scores_cache(self._scores_cache)
        
        self._cache_loaded_event.set()
        logger.info("Background cache load completed in %.2fs", time.time() - t0)
        
        # Notify JS that the data is ready
        if self.window:
            self.window.evaluate_js("if(window.fetchData) window.fetchData()")

    def set_window(self, window):
        self.window = window
        if "pytest" in sys.modules:
            self._start_stats_polling()
        else:
            def start_polling_bg():
                self._cache_loaded_event.wait()
                self._start_stats_polling()
            threading.Thread(target=start_polling_bg, daemon=True).start()

    def _get_stats_dir(self):
        return self._cfg.get("stats_dir", "")
        
    def _update_status(self, msg):
        logger.info(msg)
        if self.window:
            safe_msg = json.dumps(msg)
            self.window.evaluate_js(f"if(window.setStatus) window.setStatus({safe_msg})")
            
    def _update_progress(self, current, total):
        if self.window:
            self.window.evaluate_js(f"if(window.updateProgress) window.updateProgress({current}, {total})")

    def _load_cache_and_populate(self):
        """Load the unified JSON cache and populate tabs with cached data."""
        if not self._scores_cache:
            import os
            cache_exists = os.path.exists(SCORES_CACHE)
            self._scores_cache = load_scores_cache()
            if cache_exists and not self._scores_cache:
                self._cache_corrupted = True

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
        expected_gains = []
        candidate_sum_entries = 0
        candidate_sum_current_pts = 0

        aim_type_pcts = {}
        for lid, info in scenario_info.items():
            if (u_data := user_by_lid.get(lid)) and (entries := safe_int(info.get("entries", 0))) > 0:
                if (rank := safe_int(u_data.get("rank"))) is not None:
                    aim_type = _clean_aim_type(info.get("aimType"), info.get("name"))
                    aim_type_pcts.setdefault(aim_type, []).append((1 - rank / entries) * 100)

        aim_type_avgs = {atype: sum(pcts) / len(pcts) for atype, pcts in aim_type_pcts.items()}
        all_pcts = [p for pcts in aim_type_pcts.values() for p in pcts]
        global_avg_pct = sum(all_pcts) / len(all_pcts) if all_pcts else 50.0
        self._global_avg_pct = global_avg_pct

        stats_dir = self._get_stats_dir()
        # Use cached local stats unless marked dirty
        if self._local_stats_dirty:
            self._local_stats_cache = _get_local_stats(stats_dir, self._scores_cache)
            self._local_stats_dirty = False
        local_stats = self._local_stats_cache
        now = datetime.datetime.now()
        entry_history = self._scores_cache.get("entry_history", {})

        show_hidden = self._filters.get("hidden").get() if "hidden" in self._filters else False

        import re
        re_non_alnum = re.compile(r'[^a-z0-9]')

        for lid, info in scenario_info.items():
            sname = info["name"]
            norm_name = re_non_alnum.sub('', sname.lower())
            
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
            if hist and len(hist) >= 2:
                try:
                    dates = sorted(hist.keys())
                    oldest = _parse_iso_dt(dates[0])
                    newest = _parse_iso_dt(dates[-1])
                    seconds_diff = (newest - oldest).total_seconds()
                    if seconds_diff >= 1800:  # Need at least 30 minutes
                        popularity_trend = (hist[dates[-1]] - hist[dates[0]]) / (seconds_diff / 86400.0)
                        
                        target_24h = newest - datetime.timedelta(days=1)
                        idx_24h = 0
                        for i in range(len(dates) - 1, -1, -1):
                            if _parse_iso_dt(dates[i]) <= target_24h:
                                idx_24h = i
                                break
                        actual_new_entries = hist[dates[-1]] - hist[dates[idx_24h]]
                except ValueError:
                    pass

            competition_multiplier = max(0.2, math.log10(max(1.0, popularity_trend + 1.0)) / 2.0)

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
                cleaned_aim_type = _clean_aim_type(info.get("aimType"), info.get("name"))
                expected_pct = aim_type_avgs.get(cleaned_aim_type, global_avg_pct)
                expected_rank = max(1, int(e_val * (1.0 - expected_pct / 100.0)))
                if has_user:
                    r_val = int(user_by_lid[lid]["rank"])
                    self._global_points_sum += (e_val - r_val)
                    self._global_potential_points_sum += (r_val - 1)
                    gain = r_val - expected_rank
                    if gain > 0:
                        self._global_projected_gain_sum += gain
                        row["_projected_gain"] = gain
                        expected_gains.append(gain)
                        candidate_sum_entries += e_val
                        candidate_sum_current_pts += (e_val - r_val)
                else:
                    self._global_potential_points_sum += (e_val - 1)
                    gain = e_val - expected_rank
                    self._global_projected_gain_sum += gain
                    row["_projected_gain"] = gain
                    expected_gains.append(gain)
                    candidate_sum_entries += e_val
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

                        # Calculate Potential Score (using category-specific expected percentile)
                        potential = calculate_potential_score(
                            rank, entries, lstats, now, competition_multiplier, expected_pct=expected_pct
                        )
                        row["Potential"] = f"{potential}"

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
        self._scenarios_expected_gains = sorted(expected_gains, reverse=True)
        self._candidate_sum_entries = candidate_sum_entries
        self._candidate_sum_current_pts = candidate_sum_current_pts
        return played_rows, unplayed_rows

    # -------------------------------------------------------------------
    # Thread-safe helpers
    # -------------------------------------------------------------------




    def _rebuild_data_and_finish(self, errors=0, silent=False, msg=None):
        if msg is None:
            msg = f"Fetch complete with {errors} errors."
        self._update_status(msg)
        self._update_progress(1.0, 1.0)
        if self.window:
            import json
            self.window.evaluate_js(f"fetchData({json.dumps(silent)})")

    def _rebuild_data_and_cancelled(self, silent=False):
        self._update_status("Fetch cancelled.")
        self._update_progress(0.0, 1.0)
        if self.window:
            import json
            self.window.evaluate_js(f"fetchData({json.dumps(silent)})")

    def _record_history_points(self, scenarios_list):
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        now_str = now.isoformat()
        history = self._scores_cache.get("entry_history", {})

        for lid, points in history.items():
            for k in list(points.keys()):
                try:
                    if (_parse_iso_dt(k) - now).total_seconds() > 3600:
                        del points[k]
                except ValueError:
                    pass

        total_scenarios = len(scenarios_list)
        for idx, s in enumerate(scenarios_list):
            lid = str(s.get("leaderboardId", ""))
            try:
                entries = int(s.get("counts", {}).get("entries", 0))
            except (ValueError, TypeError):
                continue
            lid_history = history.setdefault(lid, {})
            if lid_history:
                latest_key = max(lid_history.keys())
                try:
                    if (now - _parse_iso_dt(latest_key)).total_seconds() < 3600:
                        lid_history[latest_key] = entries
                        continue
                except ValueError:
                    pass
            lid_history[now_str] = entries
            while len(lid_history) > 168:
                del lid_history[min(lid_history.keys())]
            
            if idx % 1000 == 0 and hasattr(self, "_update_progress"):
                progress = 0.15 + 0.07 * (idx / total_scenarios if total_scenarios > 0 else 0)
                self._update_progress(progress, 1.0)

        self._scores_cache["entry_history"] = history

    def get_data(self, min_entries, show_hidden=False):
        self._cache_loaded_event.wait()
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
            
            cached = self._scores_cache.get("next_rank", {})
            cached_pts = cached.get("points")
            cached_user = cached.get("username")
            cached_time = cached.get("timestamp", 0)
            
            import time
            now = time.time()
            
            def fetch_and_update():
                try:
                    import kovaaks.api as api
                    next_points = api.get_next_leaderboard_position_points(username, current_points)
                    if next_points and next_points > current_points:
                        self._scores_cache["next_rank"] = {
                            "username": username,
                            "points": next_points,
                            "timestamp": time.time()
                        }
                        if not getattr(self, "_cache_corrupted", False):
                            save_scores_cache(self._scores_cache)
                        diff = int(next_points - current_points)
                        if hasattr(self, 'window') and self.window:
                            self.window.evaluate_js(f"if(document.getElementById('stat-next-rank')) document.getElementById('stat-next-rank').textContent = '+{diff:,}';")
                            val_dict = self.get_scenarios_left_to_next_rank()
                            self.window.evaluate_js(f"""
                                if(document.getElementById('stat-scenarios-left')) document.getElementById('stat-scenarios-left').textContent = {json.dumps(val_dict.get('count'))};
                                if(document.getElementById('stat-current-pct')) document.getElementById('stat-current-pct').textContent = {json.dumps(val_dict.get('global_avg_pct'))};
                                if(document.getElementById('stat-required-pct')) document.getElementById('stat-required-pct').textContent = {json.dumps(val_dict.get('required_avg_pct'))};
                            """)
                except Exception as e:
                    logger.warning("Background next rank fetch failed: %s", e)

            # If cache is valid for this user and current points
            if cached_user == username and cached_pts and cached_pts > current_points:
                # Refresh in background if older than 1 hour to keep threshold reasonably fresh
                if now - cached_time > 3600:
                    import threading
                    threading.Thread(target=fetch_and_update, daemon=True).start()
                diff = int(cached_pts - current_points)
                return f"+{diff:,}"

            # Cache miss or user surpassed the old cached threshold, fetch synchronously
            import kovaaks.api as api
            next_points = api.get_next_leaderboard_position_points(username, current_points)
            if next_points and next_points > current_points:
                self._scores_cache["next_rank"] = {
                    "username": username,
                    "points": next_points,
                    "timestamp": now
                }
                save_scores_cache(self._scores_cache)
                diff = int(next_points - current_points)
                return f"+{diff:,}"
            else:
                return "Rank 1!"
        except Exception as e:
            logger.warning("Error fetching next rank points: %s", e)
            return "Error"

    def get_scenarios_left_to_next_rank(self):
        try:
            current_points = getattr(self, '_global_points_sum', 0)
            if current_points <= 0:
                return {"count": "N/A", "global_avg_pct": "N/A", "required_avg_pct": "N/A"}
            username = self._cfg.get("username", "").strip()
            if not username:
                return {"count": "N/A", "global_avg_pct": "N/A", "required_avg_pct": "N/A"}
            
            cached = self._scores_cache.get("next_rank", {})
            cached_pts = cached.get("points")
            cached_user = cached.get("username")
            
            if cached_user != username or not cached_pts:
                return {"count": "N/A", "global_avg_pct": "N/A", "required_avg_pct": "N/A"}
                
            diff = int(cached_pts - current_points)
            
            global_avg_pct_val = getattr(self, '_global_avg_pct', 0.0)
            global_avg_pct_str = f"{global_avg_pct_val:.2f}%"
            
            if diff <= 0:
                return {"count": "0", "global_avg_pct": global_avg_pct_str, "required_avg_pct": "0.00%"}
                
            if not hasattr(self, '_scenarios_expected_gains') or not self._scenarios_expected_gains:
                return {"count": "N/A", "global_avg_pct": global_avg_pct_str, "required_avg_pct": "N/A"}
                
            sum_gains = 0
            count = 0
            count_str = f">{len(self._scenarios_expected_gains)}"
            for gain in self._scenarios_expected_gains:
                sum_gains += gain
                count += 1
                if sum_gains >= diff:
                    count_str = str(count)
                    break
                
            # Calculate required average percentile
            sum_entries = getattr(self, '_candidate_sum_entries', 0)
            sum_current_pts = getattr(self, '_candidate_sum_current_pts', 0)
            if sum_entries > 0:
                required_pct_val = 100.0 * (diff + sum_current_pts) / sum_entries
                if required_pct_val > 100.0:
                    required_pct_str = ">100%"
                elif required_pct_val <= 0.0:
                    required_pct_str = "0.00%"
                else:
                    required_pct_str = f"{required_pct_val:.2f}%"
            else:
                required_pct_str = "N/A"
                
            return {
                "count": count_str,
                "global_avg_pct": global_avg_pct_str,
                "required_avg_pct": required_pct_str
            }
        except Exception as e:
            logger.warning("Error calculating scenarios left: %s", e)
            return {"count": "Error", "global_avg_pct": "Error", "required_avg_pct": "Error"}

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
        lid = next((k for k, v in self._scenario_info.items() if v["name"] == scenario_name), None)
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
            "refresh_interval": self._cfg.get("refresh_interval", 2),
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
                    self._known_stat_files.update(f for f in os.listdir(stats_dir) if f.endswith(" Stats.csv"))
                except OSError:
                    pass
            self._scores_cache["known_stat_files"] = list(self._known_stat_files)
            save_scores_cache(self._scores_cache)
            self._local_stats_dirty = True

    def save_credentials(self, username, password):
        from kovaaks.config_helpers import save_config
        self._cfg["username"] = username
        self._cfg["password"] = password
        save_config(self._cfg)

    def get_clipboard(self):
        import sys
        import subprocess

        # 1. macOS fallback using pbpaste
        if sys.platform == "darwin":
            try:
                return subprocess.check_output(["pbpaste"], text=True)
            except Exception:
                pass

        # 2. Linux fallback using xclip or xsel
        elif sys.platform.startswith("linux"):
            for cmd in [["xclip", "-selection", "clipboard", "-o"], ["xsel", "-b", "-o"]]:
                try:
                    return subprocess.check_output(cmd, text=True)
                except Exception:
                    continue

        # 3. Windows fallback using ctypes
        elif sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                
                OpenClipboard = ctypes.windll.user32.OpenClipboard
                OpenClipboard.argtypes = [wintypes.HWND]
                OpenClipboard.restype = wintypes.BOOL
                
                GetClipboardData = ctypes.windll.user32.GetClipboardData
                GetClipboardData.argtypes = [wintypes.UINT]
                GetClipboardData.restype = wintypes.HANDLE
                
                CloseClipboard = ctypes.windll.user32.CloseClipboard
                CloseClipboard.argtypes = []
                CloseClipboard.restype = wintypes.BOOL
                
                GlobalLock = ctypes.windll.kernel32.GlobalLock
                GlobalLock.argtypes = [wintypes.HANDLE]
                GlobalLock.restype = ctypes.c_void_p
                
                GlobalUnlock = ctypes.windll.kernel32.GlobalUnlock
                GlobalUnlock.argtypes = [wintypes.HANDLE]
                GlobalUnlock.restype = wintypes.BOOL
                
                CF_UNICODETEXT = 13
                
                if OpenClipboard(None):
                    try:
                        h_clip_mem = GetClipboardData(CF_UNICODETEXT)
                        if h_clip_mem:
                            p_clip_mem = GlobalLock(h_clip_mem)
                            if p_clip_mem:
                                try:
                                    text = ctypes.c_wchar_p(p_clip_mem).value
                                    return text or ""
                                finally:
                                    GlobalUnlock(h_clip_mem)
                    finally:
                        CloseClipboard()
            except Exception:
                pass

        # 4. Final fallback using tkinter
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

    def fetch_all_stats(self, silent=False):
        if getattr(self, "_fetch_in_progress", False):
            logger.info("Fetch already in progress, skipping.")
            return False
        self._fetch_in_progress = True
        self._fetch_cancelled = False
        username = self._cfg.get("username", "")
        password = self._cfg.get("password", "")
        threading.Thread(target=run_fetch_all, args=(self, username, password, silent), daemon=True).start()
        return True

    def is_fetch_in_progress(self):
        return getattr(self, "_fetch_in_progress", False)

    def cancel_fetch(self):
        if getattr(self, "_fetch_in_progress", False):
            self._fetch_cancelled = True
            logger.info("Cancellation requested for the current fetch.")
            return True
        return False

    def play_scenario(self, name):
        import urllib.parse
        import webbrowser
        from kovaaks.constants import STEAM_LAUNCH_URI
        from kovaaks.api import is_scenario_zombie

        if not hasattr(self, "_zombies"):
            self._zombies = set(self._scores_cache.setdefault("zombies", []))

        stats_dir = self._get_stats_dir()

        import re
        norm_name = re.sub(r'[^a-z0-9]', '', name.lower())

        if norm_name in self._zombies:
            self._update_status(f"Error: '{name}' has been deleted from Steam Workshop.")
            logger.warning("Scenario '%s' is a zombie scenario (deleted from Steam Workshop).", name)
            if self.window:
                import json
                safe_name = json.dumps(name)
                self.window.evaluate_js(f"if(window.onZombieDetected) window.onZombieDetected({safe_name})")
            return True

        # Optimistically launch the scenario immediately
        try:
            uri = STEAM_LAUNCH_URI.format(urllib.parse.quote(name, safe=""))
            self._update_status(f"Launching: {name}")
            webbrowser.open(uri)
        except Exception as e:
            logger.exception("Error launching scenario: %s", name)
            self._update_status(f"Error launching: {name}")
            return True

        # Check in the background if it's a zombie to update our cache
        def check_zombie_bg():
            try:
                is_zombie = is_scenario_zombie(name, stats_dir, self._zombies)
                download_failed = False
                
                from kovaaks.api import is_scenario_downloaded
                
                # Re-verify if it downloaded while the zombie check was running
                if is_zombie and is_scenario_downloaded(name, stats_dir):
                    logger.info("Scenario '%s' was flagged as zombie, but was found locally. Clearing zombie flag.", name)
                    is_zombie = False
                
                if not is_zombie:
                    if not is_scenario_downloaded(name, stats_dir):
                        import time
                        downloaded = False
                        for _ in range(60):
                            time.sleep(1)
                            if is_scenario_downloaded(name, stats_dir):
                                downloaded = True
                                break
                        if not downloaded:
                            logger.warning("Scenario '%s' failed to download locally within 60s. Skipping.", name)
                            download_failed = True

                if is_zombie or download_failed:
                    if norm_name not in self._zombies:
                        self._zombies.add(norm_name)
                        self._scores_cache["zombies"] = list(self._zombies)
                        save_scores_cache(self._scores_cache)
                    
                    reason = "deleted from Steam Workshop" if is_zombie else "download failed or timed out"
                    self._update_status(f"Error: '{name}' has {reason}.")
                    
                    if self.window:
                        import json
                        safe_name = json.dumps(name)
                        self.window.evaluate_js(f"if(window.onZombieDetected) window.onZombieDetected({safe_name})")
                        self.window.evaluate_js("if(window.fetchData) window.fetchData()")
            except Exception as e:
                logger.error("Error in background zombie check for '%s': %s", name, e)

        threading.Thread(target=check_zombie_bg, daemon=True).start()
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
        
        self._start_file_watcher()

    def _start_file_watcher(self):
        """Start a watchdog-based file watcher for near-instant detection (~10ms).
        Falls back to mtime-based polling (250ms) if watchdog is unavailable."""
        stats_dir = self._get_stats_dir()
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            api_ref = self

            class StatsFileHandler(FileSystemEventHandler):
                def on_created(self, event):
                    if event.is_directory:
                        return
                    fname = os.path.basename(event.src_path)
                    if not fname.endswith(" Stats.csv"):
                        return
                    if fname in api_ref._known_stat_files:
                        return
                    api_ref._known_stat_files.add(fname)
                    api_ref._scores_cache["known_stat_files"] = list(api_ref._known_stat_files)
                    
                    # Save cache in the background to avoid blocking the file event handler thread
                    threading.Thread(
                        target=save_scores_cache,
                        args=(api_ref._scores_cache,),
                        daemon=True
                    ).start()

                    api_ref._local_stats_dirty = True
                    threading.Thread(
                        target=api_ref._handle_new_stats_files,
                        args=(stats_dir, {fname}),
                        daemon=True
                    ).start()

            if stats_dir and os.path.exists(stats_dir):
                observer = Observer()
                observer.schedule(StatsFileHandler(), stats_dir, recursive=False)
                observer.daemon = True
                observer.start()
                logger.info("Stats watcher: using watchdog/inotify for instant detection")
                return
        except ImportError:
            pass
        except Exception as e:
            logger.debug("Watchdog observer failed, falling back to polling: %s", e)

        # Fallback: mtime-based polling loop
        logger.info("Stats watcher: using mtime polling (250ms interval)")
        threading.Thread(target=self._poll_stats_loop, daemon=True).start()

    def _poll_stats_loop(self):
        """Fallback polling loop using directory mtime for change detection."""
        last_mtime = 0
        stats_dir = self._get_stats_dir()
        if stats_dir and os.path.exists(stats_dir):
            try:
                last_mtime = os.stat(stats_dir).st_mtime
            except OSError:
                pass

        while True:
            time.sleep(0.25)
            stats_dir = self._get_stats_dir()
            if not stats_dir or not os.path.exists(stats_dir):
                continue
            try:
                # Fast check using directory modification time
                mtime = os.stat(stats_dir).st_mtime
                if mtime == last_mtime:
                    continue
                last_mtime = mtime

                current_files = set(f for f in os.listdir(stats_dir) if f.endswith(" Stats.csv"))
                new_files = current_files - self._known_stat_files
                if new_files:
                    self._known_stat_files.update(new_files)
                    self._scores_cache["known_stat_files"] = list(self._known_stat_files)
                    
                    # Save cache in the background to avoid blocking the polling thread
                    threading.Thread(
                        target=save_scores_cache,
                        args=(self._scores_cache,),
                        daemon=True
                    ).start()
                    
                    self._local_stats_dirty = True
                    threading.Thread(
                        target=self._handle_new_stats_files,
                        args=(stats_dir, new_files),
                        daemon=True
                    ).start()
            except OSError:
                pass

    def _handle_new_stats_files(self, stats_dir, new_files):
        # Extract scenario names from filenames immediately (no file I/O needed)
        snames = set()
        for fname in new_files:
            base = fname[:-10]
            parts = base.rsplit(" - ", 2)
            if len(parts) >= 3:
                snames.add(parts[0])

        # Notify autoplay FIRST — this is the latency-critical path.
        # onLocalScoreDetected triggers autoplayAdvance() which launches the next
        # scenario. This must happen before the heavy fetchData() table rebuild.
        for sname in snames:
            if self.window:
                import json
                safe_sname = json.dumps(sname)
                self.window.evaluate_js(f"if (window.onLocalScoreDetected) window.onLocalScoreDetected({safe_sname})")

        # Notify JS to reload the table data (to show the updated local runs count).
        # This triggers a full table rebuild, so it comes after autoplay notification.
        if self.window:
            self.window.evaluate_js("if(window.fetchData) window.fetchData()")

        # Now parse score values from the files (can afford to wait/retry here)
        lids_to_update = {}  # lid -> expected_new_score

        # Build reverse index once for fast name→lid lookup
        name_to_lid = {}
        for lid, info in self._scenario_info.items():
            name_to_lid[info["name"]] = lid

        for fname in new_files:
            base = fname[:-10]
            parts = base.rsplit(" - ", 2)
            if len(parts) >= 3:
                sname = parts[0]
                lid = name_to_lid.get(sname)
                if lid is None:
                    continue

                fpath = os.path.join(stats_dir, fname)
                score_val = None
                for attempt in range(3):
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                            for line in f:
                                if line.startswith("Score:,"):
                                    score_val = float(line.split(",")[1])
                                    break
                        if score_val is not None:
                            break
                    except Exception:
                        pass
                    time.sleep(0.1)

                if score_val is not None:
                    lids_to_update[lid] = max(lids_to_update.get(lid, -999999.0), score_val)
                elif lid not in lids_to_update:
                    lids_to_update[lid] = -999999.0

        if not lids_to_update:
            return

        # Brief pause to let the KovaaKs client upload the stats to the server.
        # The client typically uploads within ~1s of writing the stats file.
        time.sleep(1)

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

        session = requests.Session()
        for lid, expected_score in lids_to_update.items():
            max_attempts = 5
            for attempt in range(max_attempts):
                try:
                    data = kovaaks_get_friends_scores(
                        self._jwt_token, lid, session=session,
                        timeout=10, max_retries=2)
                    
                    user_entry, friend_entries = parse_leaderboard_entries(data, username)
                    
                    target_met = True
                    if expected_score > -999999.0:
                        if not user_entry:
                            target_met = False
                        else:
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
                        # Exponential backoff: 1s, 2s, 3s, 4s
                        retry_wait = min(4, attempt + 1)
                        logger.debug("Score for lid=%s not updated yet in API, retrying (%d/%d) in %ds...", lid, attempt+1, max_attempts, retry_wait)
                        time.sleep(retry_wait)
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
                    
                    retry_wait = min(4, attempt + 1)
                    logger.debug("Failed auto-update for lid=%s on attempt %d/%d: %s", lid, attempt+1, max_attempts, e)
                    time.sleep(retry_wait)
                
        if updated:
            # Notify JS immediately to refresh the table with the new in-memory scores
            if self.window:
                self.window.evaluate_js("if(window.fetchData) window.fetchData()")
            # Save the updated scores cache in the background (non-blocking)
            threading.Thread(
                target=save_scores_cache,
                args=(self._scores_cache,),
                daemon=True
            ).start()




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
