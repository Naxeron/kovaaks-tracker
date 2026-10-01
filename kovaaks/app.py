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
from copy import deepcopy

from kovaaks.constants import MIN_ENTRIES
from kovaaks.config_helpers import load_config
from kovaaks import credentials
from kovaaks.cache import CacheWriter, load_scores_cache, load_scenarios_from_cache, save_scores_cache, SCORES_CACHE
from kovaaks.history import CompactHistory
from kovaaks.memory import log_memory
from kovaaks.scoring import calculate_potential_score, parse_popularity_metrics, prune_entry_history
from kovaaks.stats import get_local_stats as _get_local_stats
from kovaaks.fetch_worker import run_fetch_all
from kovaaks.data_processing import safe_int, safe_float

from kovaaks import logging_helpers

logger = logging_helpers.setup_logging()

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
        self._shutdown_event = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False
        self._fetch_thread = None
        self._watcher_cleanup_thread = None
        self._data_lock = threading.RLock()
        self._cfg = load_config()
        self._credentials_lock = threading.RLock()
        self._credentials_loaded_event = threading.Event()
        self._password = self._cfg.pop("password", "")
        self._legacy_migration_pending = bool(self._password)
        self._credential_storage = "empty"
        self._credential_message = "No password saved."
        self._credential_warning = False
        self._credential_generation = 0
        self._scores_cache = {}
        self._cache_writer = CacheWriter(
            self._cache_snapshot, save=self._persist_cache_snapshot,
            synchronous="pytest" in sys.modules,
        )
        self._next_rank_lock = threading.Lock()
        self._next_rank_requests = {}
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
        self._watcher_observer = None
        
        self._cache_loaded_event = threading.Event()
        if "pytest" in sys.modules:
            self._initial_credentials_load()
            self._load_cache_and_populate()
            # Perform initial local parsing in tests to keep behavior synchronous.
            self._refresh_local_stats()
            self._cache_loaded_event.set()
        else:
            threading.Thread(target=self._initial_credentials_load, daemon=True).start()
            threading.Thread(target=self._initial_cache_load, daemon=True).start()

    def _initial_credentials_load(self):
        """Unlock credentials off the GUI thread and always release API callers."""
        try:
            with self._credentials_lock:
                username = self._cfg.get("username", "")
                if self._legacy_migration_pending:
                    self._store_password(username, self._password)
                    if not self._legacy_migration_pending:
                        from kovaaks.config_helpers import save_config
                        try:
                            save_config(self._cfg)
                        except OSError:
                            self._credential_warning = True
                            self._credential_message = (
                                "Password saved securely, but the old password could not be "
                                "removed from the settings file. Save settings to retry."
                            )
                else:
                    self._load_password(username)
        except Exception:
            # Credential backend exceptions can contain secrets; never log them.
            self._credential_storage = "session" if self._password else "unavailable"
            self._credential_message = "Secure storage is unavailable. Please enter your password again."
            logger.warning("Credential initialization failed; secure storage is unavailable.")
        finally:
            self._credentials_loaded_event.set()

    def _load_password(self, username):
        """Load only this account's password; callers hold the credential lock."""
        self._password = ""
        self._credential_storage = "empty"
        self._credential_message = "No password saved."
        self._credential_warning = False
        if not username:
            return
        try:
            self._password = credentials.get_password(username) or ""
            if self._password:
                self._credential_storage = "saved"
                self._credential_message = "Password saved securely on this device."
        except credentials.CredentialStorageError:
            self._credential_storage = "unavailable"
            self._credential_message = "Secure storage is unavailable or locked. Enter your password to continue."

    def _store_password(self, username, password):
        """Keep login usable for this session if the OS store cannot save it."""
        self._password = password
        self._credential_warning = False
        try:
            credentials.set_password(username, password)
        except credentials.CredentialStorageError:
            self._credential_storage = "session"
            self._credential_message = (
                "Secure storage is unavailable or locked. Your password will be used for this session only."
            )
            if self._legacy_migration_pending:
                self._credential_message += (
                    " The old settings file is unchanged until you save or forget the password."
                )
        else:
            self._legacy_migration_pending = False
            self._credential_storage = "saved"
            self._credential_message = "Password saved securely on this device."

    def _get_login_credentials(self):
        """Return a consistent account/password pair after startup unlocking."""
        self._credentials_loaded_event.wait()
        with self._credentials_lock:
            return self._cfg.get("username", ""), self._password

    def _login_for_generation(self, username, password, generation):
        """Discard a login that finished after the account was changed or forgotten."""
        from kovaaks.api import kovaaks_login
        token = kovaaks_login(username, password)
        with self._credentials_lock:
            if self._shutdown_event.is_set() or generation != self._credential_generation:
                return None
            self._jwt_token = token
            return token

    def _credential_result(self, ok=True, message=None):
        return {"ok": ok, "credential_storage": self._credential_storage,
                "has_password": bool(self._password),
                "credential_warning": self._credential_warning,
                "message": self._credential_message if message is None else message}

    def _persist_config(self):
        """Preserve unmigrated legacy credentials until securely saved or forgotten."""
        from kovaaks.config_helpers import save_config
        if self._legacy_migration_pending:
            return False
        save_config(self._cfg)
        if self._credential_storage == "saved":
            self._credential_warning = False
            self._credential_message = "Password saved securely on this device."
        return True

    def _initial_cache_load(self):
        """Load cached data without leaving callers blocked after a failure."""
        logger.info("Starting background cache load...")
        t0 = time.time()
        try:
            self._load_cache_and_populate()

            # Parse local runs before starting the watcher. Build rows only when
            # the browser requests them, with its current filters, after readiness.
            self._refresh_local_stats()
            status = f"Loaded memory cache — {len(self._scenario_info)} scenarios"

            # Save the updated scores cache in case get_local_stats added new local runs.
            # Publish readiness before compression so cached rows are usable immediately.
            self._cache_loaded_event.set()
            if self._scores_cache.pop("_dirty", False):
                self._queue_cache_save()
        except Exception:
            logger.exception("Background cache load failed")
            status = "Could not load cached data. Check the logs and refresh to retry."
        finally:
            self._cache_loaded_event.set()
            logger.info("Background cache load completed in %.2fs", time.time() - t0)
            try:
                self._update_status(status)
            except Exception:
                logger.debug("Startup status notification failed", exc_info=True)
            try:
                self._update_progress(1, 1)
            except Exception:
                logger.debug("Startup progress notification failed", exc_info=True)

            # The browser's initial get_data call waits on readiness already;
            # another fetchData notification would queue a duplicate rebuild.

    def set_window(self, window):
        self.window = window
        window.events.closing += self._begin_shutdown
        if "pytest" in sys.modules:
            self._start_stats_polling()
        else:
            def start_polling_bg():
                self._cache_loaded_event.wait()
                self._start_stats_polling()
            threading.Thread(target=start_polling_bg, daemon=True).start()

    def _begin_shutdown(self):
        """Cancel work before the browser disappears, without blocking its loop."""
        with self._lifecycle_lock:
            if self._shutdown_event.is_set():
                return
            self._shutdown_event.set()
            self._fetch_cancelled = True
            self.window = None
            if self._watcher_observer is not None:
                observer = self._watcher_observer

                def stop_watcher():
                    # Watchdog.stop can itself join emitters without a timeout.
                    # Keep it off the GUI and shutdown threads as well.
                    try:
                        observer.stop()
                    except Exception:
                        logger.exception("Could not stop stats watcher")
                    finally:
                        try:
                            observer.join(timeout=1.0)
                        except Exception:
                            logger.exception("Could not join stats watcher")

                self._watcher_cleanup_thread = threading.Thread(target=stop_watcher, daemon=True)
                self._watcher_cleanup_thread.start()

    def shutdown(self):
        """Finish accepted cache updates after the GUI loop has stopped.

        Network workers are cancellable daemon threads. A request stuck in the
        OS must not delay exit; give the fetch coordinator a bounded chance to
        checkpoint, then persist any remaining dirty data ourselves.
        """
        self._begin_shutdown()
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            try:
                if self._fetch_thread is not None:
                    self._fetch_thread.join(timeout=2.0)
                if self._watcher_cleanup_thread is not None:
                    self._watcher_cleanup_thread.join(timeout=1.0)
            finally:
                try:
                    saved = self._flush_cache_saves()
                    # Never replace a cache that is still loading or corrupt.
                    if self._cache_loaded_event.is_set():
                        with self._data_lock:
                            dirty = self._scores_cache.pop("_dirty", False)
                        if dirty or not saved:
                            self._queue_cache_save(wait=True)
                finally:
                    self._shutdown_complete = True

    def _get_stats_dir(self):
        from kovaaks.config_helpers import get_default_stats_dir
        stats_dir = self._cfg.get("stats_dir")
        if not stats_dir:
            stats_dir = get_default_stats_dir()
        return os.path.expanduser(stats_dir) if stats_dir else ""
        
    def _update_status(self, msg):
        logger.info(msg)
        if self.window:
            safe_msg = json.dumps(msg)
            self.window.evaluate_js(f"if(window.setStatus) window.setStatus({safe_msg})")
            
    def _update_progress(self, current, total):
        if self.window:
            self.window.evaluate_js(f"if(window.updateProgress) window.updateProgress({current}, {total})")

    def _cache_snapshot(self):
        """Detach a consistent snapshot before the writer performs compression."""
        with self._data_lock:
            if getattr(self, "_cache_corrupted", False):
                return None
            snapshot = deepcopy(self._scores_cache)
        log_memory("cache snapshot")
        return snapshot

    def _queue_cache_save(self, wait=False):
        """Serialize saves and coalesce pending updates; wait outside data locks."""
        if getattr(self, "_cache_corrupted", False):
            return False
        with self._data_lock:
            self._scores_cache.pop("_dirty", None)
        saved = self._cache_writer.request(wait=wait)
        if not saved:
            with self._data_lock:
                self._scores_cache["_dirty"] = True
        return saved

    def _persist_cache_snapshot(self, snapshot):
        """Retain dirty state on asynchronous failures so later refreshes retry."""
        saved = False
        try:
            saved = save_scores_cache(snapshot) is not False
            return saved
        finally:
            if not saved:
                with self._data_lock:
                    self._scores_cache["_dirty"] = True

    def _flush_cache_saves(self):
        """Wait for already requested saves without scheduling another rewrite."""
        saved = self._cache_writer.flush()
        if not saved:
            with self._data_lock:
                self._scores_cache["_dirty"] = True
        return saved

    def _load_cache_and_populate(self):
        """Load the unified JSON cache and populate tabs with cached data."""
        if not self._scores_cache:
            import os
            cache_exists = os.path.exists(SCORES_CACHE)
            self._scores_cache = load_scores_cache()
            if cache_exists and not self._scores_cache:
                self._cache_corrupted = True
            if prune_entry_history(self._scores_cache.get("entry_history", {})):
                self._scores_cache["_dirty"] = True

        self._zombies = set(self._scores_cache.setdefault("zombies", []))
        all_scenarios = load_scenarios_from_cache(self._scores_cache)

        # Filter to config-defined min entries
        min_entries_threshold = safe_int(self._cfg.get("min_entries", MIN_ENTRIES), MIN_ENTRIES)
        master = []
        for s in all_scenarios:
            entries = s.get("counts", {}).get("entries", 0)
            try:
                entries = int(entries)
            except (ValueError, TypeError):
                entries = 0
            if entries >= min_entries_threshold:
                master.append(s)

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

        # Replace these maps even when empty to clear stale filtered rows.
        self._scenario_info = scenario_info
        self._user_by_lid = user_by_lid
        self._friends_by_lid = friends_by_lid

    # -------------------------------------------------------------------
    # Settings
    # -------------------------------------------------------------------


    def _rebuild_data(self):
        """Build unified row list from current data and update the UI."""
        with self._data_lock:
            return self._build_data_rows()

    def _invalidate_local_stats(self):
        """Keep file events from being lost while another refresh parses stats."""
        with self._data_lock:
            self._local_stats_dirty = True

    def _refresh_local_stats(self):
        """Parse pending local runs once, sharing startup work with row builds."""
        with self._data_lock:
            # Use cached local stats unless marked dirty.
            if self._local_stats_dirty:
                self._local_stats_cache = _get_local_stats(self._get_stats_dir(), self._scores_cache)
                self._local_stats_dirty = False

    def _build_data_rows(self):
        """Compute rows while holding the data lock to avoid duplicate parsing."""
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

        self._refresh_local_stats()
        local_stats = self._local_stats_cache
        now = datetime.datetime.now()
        entry_history = self._scores_cache.get("entry_history", {})
        popularity_timelines = {}

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
            # The helper requires at least 30 minutes and shares parsed
            # timelines across this build without caching scenario counts.
            popularity_trend, actual_new_entries = parse_popularity_metrics(
                hist, timeline_cache=popularity_timelines,
            )

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

        # Trim imported samples for every scenario before the within-hour fast path.
        changed = prune_entry_history(history, now)

        for s in scenarios_list:
            lid = str(s.get("leaderboardId", ""))
            try:
                entries = int(s.get("counts", {}).get("entries", 0))
            except (ValueError, TypeError):
                continue
            lid_history = history.get(lid)
            if lid_history is None:
                lid_history = history[lid] = CompactHistory()
            if lid_history:
                latest_key = max(lid_history.keys())
                try:
                    if (now - _parse_iso_dt(latest_key)).total_seconds() < 3600:
                        changed = lid_history[latest_key] != entries or changed
                        lid_history[latest_key] = entries
                        continue
                except ValueError:
                    pass
            lid_history[now_str] = entries
            changed = True
            if len(lid_history) > 168:
                for stamp in sorted(lid_history)[:-168]:
                    del lid_history[stamp]
        # The caller reports progress after releasing the cache lock. Browser
        # callbacks may wait indefinitely if their window is closing.
        self._scores_cache["entry_history"] = history
        if changed:
            self._scores_cache["_dirty"] = True

    def get_data(self, min_entries, show_hidden=False):
        self._cache_loaded_event.wait()
        with self._data_lock:
            result = self._get_loaded_data(min_entries, show_hidden)
            dirty = self._scores_cache.pop("_dirty", False)
        if dirty:
            self._queue_cache_save()
        return result

    def _get_loaded_data(self, min_entries, show_hidden):
        """Serialize filter changes, local parsing, and global-stat snapshots."""
        self._cfg["min_entries"] = min_entries
        class DummyVar:
            def __init__(self, val): self.val = val
            def get(self): return self.val
        self._filters["hidden"] = DummyVar(show_hidden)
        try:
            self._load_cache_and_populate()
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
        """Share one rank lookup per account and serve stale values during refresh."""
        from concurrent.futures import Future
        from kovaaks.api import get_next_leaderboard_position_points

        current_points = getattr(self, '_global_points_sum', 0)
        if current_points <= 0:
            return "N/A"
        with self._credentials_lock:
            username = self._cfg.get("username", "").strip()
            generation = self._credential_generation
        if not username:
            return "N/A (No Username)"
        key = (username, generation)

        def number(value):
            value = safe_float(value, None)
            return value if value is not None and math.isfinite(value) else None

        def display(points, official):
            diff = int(points - (official if official is not None else current_points))
            return f"+{diff:,}" if diff > 0 else "Rank 1!"

        with self._next_rank_lock:
            cached = self._scores_cache.get("next_rank", {})
            if not isinstance(cached, dict):
                cached = {}
            cached_points = number(cached.get("points"))
            cached_official = number(cached.get("user_official_points"))
            valid = (cached.get("username") == username
                     and cached_points is not None and cached_official is not None)
            cached_display = display(cached_points, cached_official) if valid else None
            if valid and time.time() - (number(cached.get("timestamp")) or 0) <= 3600:
                return cached_display
            future = self._next_rank_requests.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._next_rank_requests[key] = future

        def fetch_and_update():
            result = "Error"
            try:
                res = get_next_leaderboard_position_points(username, current_points)
                raw_points = res.get("next_points") if isinstance(res, dict) else res
                raw_official = res.get("user_official_points") if isinstance(res, dict) else None
                points, official = number(raw_points), number(raw_official)
                if ((raw_points is not None and points is None)
                        or (raw_official is not None and official is None)):
                    raise ValueError("Invalid global leaderboard points")
                with self._credentials_lock:
                    if (self._shutdown_event.is_set() or self._credential_generation != generation
                            or self._cfg.get("username", "").strip() != username):
                        result = "N/A"
                        return
                    if points is not None:
                        with self._data_lock:
                            self._scores_cache["next_rank"] = {
                                "username": username, "points": points,
                                "user_official_points": official, "timestamp": time.time(),
                            }
                            self._scores_cache["_dirty"] = True
                        result = display(points, official)
                        self._queue_cache_save()
                    else:
                        result = "Rank 1!"
                    # Background refreshes update the labels once, independently
                    # of filtering, sorting, and column visibility changes.
                    if valid and self.window and points is not None:
                        # The JS hook invalidates older pending bridge responses
                        # and checks both the active account and display mode.
                        self.window.evaluate_js(
                            "if(window.onRankStatsUpdated) { "
                            f"void window.onRankStatsUpdated({json.dumps(username)}); }}"
                        )
            except Exception:
                logger.warning("Error fetching next rank points", exc_info=True)
            finally:
                future.set_result(result)
                with self._next_rank_lock:
                    if self._next_rank_requests.get(key) is future:
                        del self._next_rank_requests[key]

        if owner:
            if valid:
                try:
                    threading.Thread(target=fetch_and_update, daemon=True).start()
                except Exception:
                    with self._next_rank_lock:
                        self._next_rank_requests.pop(key, None)
                    future.set_result("Error")
                    logger.warning("Could not start rank refresh", exc_info=True)
            else:
                # pywebview bridge calls already run outside the GUI thread.
                fetch_and_update()
        return cached_display if valid and not future.done() else future.result()

    def get_scenarios_left_to_next_rank(self):
        try:
            current_points = getattr(self, '_global_points_sum', 0)
            if current_points <= 0:
                return {"count": "N/A", "live_gap": "N/A", "global_avg_pct": "N/A", "required_avg_pct": "N/A"}
            username = self._cfg.get("username", "").strip()
            if not username:
                return {"count": "N/A", "live_gap": "N/A", "global_avg_pct": "N/A", "required_avg_pct": "N/A"}
            
            cached = self._scores_cache.get("next_rank", {})
            cached_pts = cached.get("points")
            cached_user = cached.get("username")
            
            if cached_user != username or not cached_pts:
                return {"count": "N/A", "live_gap": "N/A", "global_avg_pct": "N/A", "required_avg_pct": "N/A"}
                
            diff = int(cached_pts - current_points)
            live_gap_str = f"+{diff:,}" if diff > 0 else "+0"
            
            global_avg_pct_val = getattr(self, '_global_avg_pct', 0.0)
            global_avg_pct_str = f"{global_avg_pct_val:.2f}%"
            
            if diff <= 0:
                return {"count": "0", "live_gap": live_gap_str, "global_avg_pct": global_avg_pct_str, "required_avg_pct": "0.00%"}
                
            if not hasattr(self, '_scenarios_expected_gains') or not self._scenarios_expected_gains:
                return {"count": "N/A", "live_gap": live_gap_str, "global_avg_pct": global_avg_pct_str, "required_avg_pct": "N/A"}
                
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
                "live_gap": live_gap_str,
                "global_avg_pct": global_avg_pct_str,
                "required_avg_pct": required_pct_str
            }
        except Exception as e:
            logger.warning("Error calculating scenarios left to next rank: %s", e)
            return {"count": "N/A", "live_gap": "N/A", "global_avg_pct": "N/A", "required_avg_pct": "N/A"}

    def get_logs(self):
        log_file = logging_helpers.LOG_FILE
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
        log_file = logging_helpers.LOG_FILE
        if os.path.exists(log_file):
            open(log_file, "w").close()
            return True
        return False

    def toggle_hide_scenario(self, scenario_name):
        self._credentials_loaded_event.wait()
        lid = next((k for k, v in self._scenario_info.items() if v["name"] == scenario_name), None)
        if not lid:
            return False

        if lid in self._hidden_scenarios:
            self._hidden_scenarios.remove(lid)
        else:
            self._hidden_scenarios.add(lid)
            
        self._cfg["hidden_scenarios"] = list(self._hidden_scenarios)
        self._persist_config()
        return True

    def get_config(self, wait_for_credentials=True):
        """Let table settings load even while the operating-system store unlocks."""
        from kovaaks.config_helpers import get_default_stats_dir
        result = {
            "username": self._cfg.get("username", ""),
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
        if wait_for_credentials:
            self._credentials_loaded_event.wait()
        elif not self._credentials_loaded_event.is_set():
            return {**result, "credentials_pending": True}
        if not self._credentials_lock.acquire(blocking=wait_for_credentials):
            return {**result, "credentials_pending": True}
        try:
            return {
                **result,
                "username": self._cfg.get("username", ""),
                "credentials_pending": False,
                "has_password": bool(self._password),
                "credential_storage": self._credential_storage,
                "credential_message": self._credential_message,
                "credential_warning": self._credential_warning,
            }
        finally:
            self._credentials_lock.release()

    def save_settings(self, settings):
        self._credentials_loaded_event.wait()
        with self._credentials_lock:
            settings = dict(settings)
            username = settings.pop("username", self._cfg.get("username", ""))
            password = settings.pop("password", "")
            if not isinstance(username, str) or not isinstance(password, str):
                return self._credential_result(False, "Enter a valid username and password.")
            username = username.strip()
            old_username = self._cfg.get("username", "")
            changed_account = username != old_username
            if password and not username:
                return self._credential_result(False, "Enter a username for this password.")
            if (changed_account or password) and getattr(self, "_fetch_in_progress", False):
                return self._credential_result(False, "Wait for the current fetch to finish before changing credentials.")
            if changed_account and self._legacy_migration_pending:
                return self._credential_result(False, "Save or forget the current password before changing accounts.")
            if changed_account or password:
                self._credential_generation += 1
                self._jwt_token = None
                self._cfg["username"] = username
                if password:
                    self._store_password(username, password)
                else:
                    self._load_password(username)
            try:
                with self._data_lock:
                    saved = self._save_settings(settings)
                    dirty = self._scores_cache.pop("_dirty", False)
                if dirty:
                    self._queue_cache_save()
            except OSError:
                return self._credential_result(False, "Could not save settings. Check file permissions and try again.")
            if not saved:
                return {**self._credential_result(False), "reason": "legacy_migration_pending"}
            return self._credential_result()

    def _save_settings(self, settings):
        """Invalidate directory-specific stats atomically with their settings."""
        old_stats_dir = self._cfg.get("stats_dir")
        self._cfg.update(settings)
        saved = self._persist_config()
        
        new_stats_dir = self._cfg.get("stats_dir")
        if old_stats_dir != new_stats_dir:
            self._known_stat_files.clear()
            stats_dir = self._get_stats_dir()
            if stats_dir and os.path.exists(stats_dir):
                try:
                    self._known_stat_files.update(f for f in os.listdir(stats_dir) if f.endswith(" Stats.csv"))
                except OSError:
                    pass
            # Parsed statistics belong to the configured directory, even when
            # the new directory contains files with the same names.
            self._scores_cache["known_stat_files"] = []
            self._scores_cache["local_stats"] = {}
            self._scores_cache["newly_played_scenarios"] = []
            self._local_stats_cache = {}
            self._scores_cache["_dirty"] = True
            self._invalidate_local_stats()
            self._start_file_watcher()
        return saved

    def save_credentials(self, username, password):
        if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
            return self._credential_result(False, "Enter a username and password.")
        result = self.save_settings({"username": username, "password": password})
        # A legacy config remains intact on migration failure, but login is
        # still usable in memory and the UI explains the session-only fallback.
        if result.get("reason") == "legacy_migration_pending":
            return self._credential_result()
        return result

    def clear_credentials(self):
        """Forget the active account's stored password and invalidate its session."""
        self._credentials_loaded_event.wait()
        with self._credentials_lock:
            if getattr(self, "_fetch_in_progress", False):
                return self._credential_result(False, "Wait for the current fetch to finish before forgetting credentials.")
            username = self._cfg.get("username", "")
            deletion_failed = False
            try:
                if username:
                    credentials.delete_password(username)
            except credentials.CredentialStorageError:
                deletion_failed = True
            self._credential_generation += 1
            self._password = ""
            self._jwt_token = None
            self._legacy_migration_pending = False
            self._credential_storage = "empty"
            self._credential_message = "Password forgotten."
            self._credential_warning = False
            try:
                self._persist_config()
            except OSError:
                self._credential_warning = True
                self._credential_message = "Session cleared, but stored credentials may remain. Check file permissions and secure storage, then try forgetting again."
                return self._credential_result(False)
            if deletion_failed:
                self._credential_storage = "unavailable"
                self._credential_message = "Session cleared, but a saved password may remain in secure storage. Unlock it and try forgetting again."
                return self._credential_result(False)
            return self._credential_result()

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
        def fetch_with_credentials():
            try:
                for ready in (self._cache_loaded_event, self._credentials_loaded_event):
                    while not ready.wait(timeout=0.1):
                        if self._shutdown_event.is_set():
                            self._fetch_in_progress = False
                            return
                if self._shutdown_event.is_set():
                    self._fetch_in_progress = False
                    return
                username, password = self._get_login_credentials()
            except Exception:
                self._fetch_in_progress = False
                try:
                    self._update_status("Could not prepare login credentials. Please try again.")
                finally:
                    self._update_progress(1, 1)
                return
            if self._shutdown_event.is_set():
                self._fetch_in_progress = False
                return
            # run_fetch_all owns completion and resets the flag in its finally.
            run_fetch_all(self, username, password, silent)
        with self._lifecycle_lock:
            if self._shutdown_event.is_set() or getattr(self, "_fetch_in_progress", False):
                return False
            self._fetch_in_progress = True
            self._fetch_cancelled = False
            self._fetch_thread = threading.Thread(target=fetch_with_credentials, daemon=True)
            try:
                self._fetch_thread.start()
            except Exception:
                self._fetch_thread = None
                self._fetch_in_progress = False
                raise
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
                    changed = False
                    with self._data_lock:
                        if norm_name not in self._zombies:
                            self._zombies.add(norm_name)
                            self._scores_cache["zombies"] = list(self._zombies)
                            changed = True
                    if changed:
                        self._queue_cache_save()
                    
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
        if self._shutdown_event.is_set():
            return
        stats_dir = self._get_stats_dir()
        if stats_dir and os.path.exists(stats_dir):
            try:
                current_files = set(f for f in os.listdir(stats_dir) if f.endswith(" Stats.csv"))
                cached_known_raw = self._scores_cache.get("known_stat_files")
                cached_known = set(cached_known_raw) if cached_known_raw is not None else set()
                new_files = current_files - cached_known
                
                self._known_stat_files = current_files
                # Observed files are separate from the parser's cached files.
                # get_local_stats marks files known only after parsing them.
                
                if new_files:
                    self._invalidate_local_stats()
                    threading.Thread(
                        target=self._handle_new_stats_files,
                        args=(stats_dir, new_files),
                        daemon=True
                    ).start()
            except Exception as e:
                logger.warning("Error scanning initial stats directory '%s': %s", stats_dir, e)
        elif stats_dir:
            logger.warning("Stats directory does not exist: %s", stats_dir)
        else:
            logger.warning("No stats directory configured or default path found.")
        
        self._start_file_watcher()

    def _start_file_watcher(self):
        """Start a watchdog-based file watcher for near-instant detection (~10ms).
        Falls back to mtime-based polling (250ms) if watchdog is unavailable."""
        if self._shutdown_event.is_set():
            return
        if getattr(self, "_watcher_observer", None) is not None:
            try:
                self._watcher_observer.stop()
                self._watcher_observer.join(timeout=1.0)
            except Exception as e:
                logger.debug("Error stopping previous watchdog observer: %s", e)
            self._watcher_observer = None

        stats_dir = self._get_stats_dir()
        if not stats_dir or not os.path.exists(stats_dir):
            logger.warning("Stats watcher: directory '%s' does not exist; fallback polling active.", stats_dir)
            threading.Thread(target=self._poll_stats_loop, daemon=True).start()
            return

        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            api_ref = self

            class StatsFileHandler(FileSystemEventHandler):
                def _process_path(self, path):
                    if not path or api_ref._shutdown_event.is_set():
                        return
                    fname = os.path.basename(path)
                    if not fname.endswith(" Stats.csv"):
                        return
                    if fname in api_ref._known_stat_files:
                        # A creation event can arrive before the CSV is complete.
                        # Retry parsing modifications without advancing autoplay again.
                        if fname not in api_ref._scores_cache.get("known_stat_files", []):
                            api_ref._invalidate_local_stats()
                            if api_ref.window:
                                threading.Thread(
                                    target=api_ref.window.evaluate_js,
                                    args=("if(window.fetchData) window.fetchData()",),
                                    daemon=True,
                                ).start()
                        return
                    api_ref._known_stat_files.add(fname)
                    
                    # Save parsed cache changes in the background during get_data,
                    # avoiding premature parsed markers in the file event handler.

                    api_ref._invalidate_local_stats()
                    threading.Thread(
                        target=api_ref._handle_new_stats_files,
                        args=(stats_dir, {fname}),
                        daemon=True
                    ).start()

                def on_created(self, event):
                    if not event.is_directory:
                        self._process_path(event.src_path)

                def on_modified(self, event):
                    if not event.is_directory:
                        self._process_path(event.src_path)

                def on_moved(self, event):
                    if not event.is_directory:
                        self._process_path(getattr(event, "dest_path", event.src_path))

            observer = Observer()
            observer.schedule(StatsFileHandler(), stats_dir, recursive=False)
            observer.daemon = True
            with self._lifecycle_lock:
                if self._shutdown_event.is_set():
                    return
                observer.start()
                self._watcher_observer = observer
            logger.info("Stats watcher: using watchdog/inotify for instant detection on '%s'", stats_dir)
            return
        except ImportError:
            pass
        except Exception as e:
            logger.debug("Watchdog observer failed, falling back to polling: %s", e)

        # Fallback: mtime-based polling loop
        logger.info("Stats watcher: using mtime polling (250ms interval) on '%s'", stats_dir)
        threading.Thread(target=self._poll_stats_loop, daemon=True).start()

    def _poll_stats_loop(self):
        """Fallback polling loop using directory mtime for change detection."""
        last_mtime = 0
        pending_mtimes = dict.fromkeys(
            self._known_stat_files - set(self._scores_cache.get("known_stat_files", []))
        )
        stats_dir = self._get_stats_dir()
        if stats_dir and os.path.exists(stats_dir):
            try:
                last_mtime = os.stat(stats_dir).st_mtime
            except OSError:
                pass

        while not self._shutdown_event.wait(0.25):
            stats_dir = self._get_stats_dir()
            if not stats_dir or not os.path.exists(stats_dir):
                continue
            try:
                # Fast check using directory modification time
                mtime = os.stat(stats_dir).st_mtime
                if mtime != last_mtime:
                    last_mtime = mtime
                    current_files = set(f for f in os.listdir(stats_dir) if f.endswith(" Stats.csv"))
                    new_files = current_files - self._known_stat_files
                else:
                    new_files = set()
                if new_files:
                    self._known_stat_files.update(new_files)
                    
                    # Save parsed cache changes in the background during get_data,
                    # avoiding premature parsed markers in the polling thread.
                    
                    self._invalidate_local_stats()
                    threading.Thread(
                        target=self._handle_new_stats_files,
                        args=(stats_dir, new_files),
                        daemon=True
                    ).start()

                # File writes do not change the directory mtime. Watch only
                # pending CSVs until their scores have been successfully parsed.
                pending_files = (set(pending_mtimes) | new_files) & self._known_stat_files
                if pending_files:
                    pending_files -= set(self._scores_cache.get("known_stat_files", []))
                signatures = {}
                changed_pending = False
                for fname in pending_files:
                    try:
                        file_stat = os.stat(os.path.join(stats_dir, fname))
                    except OSError:
                        signatures[fname] = None
                        continue
                    signature = (file_stat.st_mtime_ns, file_stat.st_size)
                    signatures[fname] = signature
                    if fname in pending_mtimes and pending_mtimes[fname] != signature:
                        changed_pending = True
                pending_mtimes = signatures
                if changed_pending:
                    self._invalidate_local_stats()
                    if self.window:
                        self.window.evaluate_js("if(window.fetchData) window.fetchData()")
            except OSError:
                pass
            except Exception as e:
                logger.warning("Stats polling failed: %s", e)

    def _handle_new_stats_files(self, stats_dir, new_files):
        if self._shutdown_event.is_set():
            return
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

        # The first table refresh may have raced an incomplete CSV. Refresh
        # again after the score-read retries, before waiting for the remote API.
        self._invalidate_local_stats()
        if self.window:
            self.window.evaluate_js("if(window.fetchData) window.fetchData()")

        # Brief pause to let the KovaaKs client upload the stats to the server.
        # The client typically uploads within ~1s of writing the stats file.
        time.sleep(1)

        self._credentials_loaded_event.wait()
        with self._credentials_lock:
            username = self._cfg.get("username", "").strip()
            password = self._password
            generation = self._credential_generation
            token = self._jwt_token

        if not token:
            if not username or not password:
                logger.info("Auto-sync: Local run detected, but cannot fetch API scores (not logged in).")
                return
            try:
                token = self._login_for_generation(username, password, generation)
                if not token:
                    return
            except Exception as e:
                logger.debug("Failed silent login during stats poll: %s", e)
                return

        updated = False
        import requests
        from kovaaks.api import RequestCancelled, kovaaks_get_friends_scores
        from kovaaks.data_processing import parse_leaderboard_entries

        session = requests.Session()
        def sync_cancelled():
            with self._credentials_lock:
                return self._shutdown_event.is_set() or generation != self._credential_generation

        for lid, expected_score in lids_to_update.items():
            max_attempts = 5
            for attempt in range(max_attempts):
                with self._credentials_lock:
                    if self._shutdown_event.is_set() or generation != self._credential_generation:
                        return
                try:
                    data = kovaaks_get_friends_scores(
                        token, lid, session=session,
                        timeout=10, max_retries=2, cancel_check=sync_cancelled)
                    
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
                        with self._credentials_lock, self._data_lock:
                            if self._shutdown_event.is_set() or generation != self._credential_generation:
                                return
                            cached_entry = self._scores_cache.setdefault("scores", {}).setdefault(lid, {})
                            if user_entry:
                                if cached_entry.get("user") != user_entry:
                                    cached_entry["user"] = user_entry
                                    updated = True
                                    sname = self._scenario_info.get(lid, {}).get("name", lid)
                                    logger.info("Auto-updated score for %s", sname)
                                self._user_by_lid[lid] = cached_entry["user"]
                            if friend_entries:
                                if cached_entry.get("friends") != friend_entries:
                                    cached_entry["friends"] = friend_entries
                                    updated = True
                                self._friends_by_lid[lid] = cached_entry["friends"]
                            if updated:
                                self._scores_cache["_dirty"] = True

                        break
                    else:
                        # Exponential backoff: 1s, 2s, 3s, 4s
                        retry_wait = min(4, attempt + 1)
                        logger.debug("Score for lid=%s not updated yet in API, retrying (%d/%d) in %ds...", lid, attempt+1, max_attempts, retry_wait)
                        time.sleep(retry_wait)
                except RequestCancelled:
                    return
                except Exception as e:
                    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None and e.response.status_code == 429:
                        # The HTTP helper already exhausted bounded retries and
                        # paused the shared budget. Do not restart that cycle.
                        logger.info("Auto-sync deferred for lid=%s after rate limiting; cached scores kept.", lid)
                        break
                    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None and e.response.status_code == 401:
                        logger.warning("Session expired during auto-update. Attempting re-login.")
                        with self._credentials_lock:
                            if self._shutdown_event.is_set() or generation != self._credential_generation:
                                return
                            self._jwt_token = None
                        if username and password:
                            try:
                                token = self._login_for_generation(username, password, generation)
                                if not token:
                                    return
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
            self._queue_cache_save()




def main():
    """Run the desktop window and always finish application shutdown."""
    api = KovaaksAPI()
    window = webview.create_window(
        "KovaaK's Scenario Tracker",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html"),
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
    try:
        webview.start(gui=gui_backend, debug=False)
    finally:
        api.shutdown()


if __name__ == "__main__":
    main()
