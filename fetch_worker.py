import time
import json
import gzip
import io
import logging
import threading
import concurrent.futures
import requests

from constants import GITHUB_RAW_BASE, MIN_ENTRIES
from api import (
    api_request_with_retry,
    fetch_all_scenarios,
    kovaaks_login,
    kovaaks_get_friends_scores,
)
from cache import save_scores_cache
from config_helpers import save_config
from data_processing import (
    get_estimated_fetch_count,
    get_estimated_matching_count,
    parse_leaderboard_entries,
)

logger = logging.getLogger("kovaaks")

def run_fetch_all(app, username, password):
    """
    Background worker that fetches all scenarios and updates the GUI state.
    `app` must be a KovaaksApp instance.
    """
    try:
        # ── Step 1: Fetch all scenarios (try GitHub first, fallback to API) ──
        app._update_status("Fetching all scenarios…")

        scores_cache = app._scores_cache
        min_entries_threshold = int(app._cfg.get("min_entries", MIN_ENTRIES))
        
        all_scenarios = None
        repo_url = f"{GITHUB_RAW_BASE}/scenarios.json.gz"
        
        try:
            logger.info("Attempting to fetch scenarios from GitHub repo: %s", repo_url)
            app._update_status("Downloading scenarios from repository…")
            resp = api_request_with_retry("get", repo_url, timeout=30)
            logger.info("GitHub response status: %d", resp.status_code)
            if resp.status_code == 200:
                etag = resp.headers.get("ETag") or resp.headers.get("Last-Modified")
                if etag:
                    if "last_etags" not in app._cfg: app._cfg["last_etags"] = {}
                    app._cfg["last_etags"]["scenarios.json.gz"] = etag
                    save_config(app._cfg)

                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as f:
                        all_scenarios = json.load(f)
                    logger.info("Successfully fetched %d scenarios from GitHub", len(all_scenarios))
                except (json.JSONDecodeError, OSError) as je:
                    logger.error("Failed to decode scenarios.json.gz: %s", je)
            else:
                logger.warning("GitHub returned non-200 status: %d", resp.status_code)
        except Exception as e:
            logger.warning("Failed to fetch scenarios from GitHub: %s", e)

        # ── Step 1b: Fetch scenario history from GitHub ──
        history_url = f"{GITHUB_RAW_BASE}/scenarios_history.json.gz"
        try:
            logger.info("Attempting to fetch history from GitHub: %s", history_url)
            resp = api_request_with_retry("get", history_url, timeout=30)
            if resp.status_code == 200:
                etag = resp.headers.get("ETag") or resp.headers.get("Last-Modified")
                if etag:
                    if "last_etags" not in app._cfg: app._cfg["last_etags"] = {}
                    app._cfg["last_etags"]["scenarios_history.json.gz"] = etag
                    save_config(app._cfg)

                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as f:
                        ext_history = json.load(f)
                    
                    h_timestamps = ext_history.get("timestamps", [])
                    h_data = ext_history.get("history", {})
                    
                    if h_timestamps and h_data:
                        local_history = scores_cache.get("entry_history", {})
                        merged_count = 0
                        for lid_raw, counts in h_data.items():
                            lid = str(lid_raw)
                            if lid not in local_history:
                                local_history[lid] = {}
                            for i, count in enumerate(counts):
                                if count is not None and i < len(h_timestamps):
                                    ts = h_timestamps[i]
                                    if ts not in local_history[lid]:
                                        local_history[lid][ts] = count
                                        merged_count += 1
                        
                        scores_cache["entry_history"] = local_history
                        logger.info("Merged %d history points from GitHub", merged_count)
                except Exception as je:
                    logger.error("Failed to parse history.json.gz: %s", je)
        except Exception as e:
            logger.warning("Failed to fetch history from GitHub: %s", e)

        if not all_scenarios:
            app._update_status("Fetching scenarios (API fallback)…")
            total_est = get_estimated_fetch_count(min_entries_threshold) + get_estimated_matching_count(min_entries_threshold)

            def stage1_callback(done, total, msg):
                app._update_status(msg)
                prog = done / total_est if total_est > 0 else 0
                app._update_progress(min(0.95, prog), 1.0)

            session = requests.Session()
            all_scenarios = fetch_all_scenarios(
                min_entries=min_entries_threshold, 
                session=session, 
                progress_callback=stage1_callback
            )
            logger.info("API returned %d total scenarios", len(all_scenarios))
            scores_cache["scenarios"] = all_scenarios
        else:
            scores_cache["scenarios"] = all_scenarios
            app._update_progress(0.4, 1.0)

        save_scores_cache(scores_cache)
        logger.info("Saved GitHub/API scenarios and history to local cache")

        master = []
        for s in all_scenarios:
            entries = s.get("counts", {}).get("entries", 0)
            try:
                entries = int(entries)
            except (ValueError, TypeError):
                entries = 0
            if entries >= min_entries_threshold:
                master.append(s)

        logger.info("Filtered to %d scenarios with >=%d entries", len(master), min_entries_threshold)
        
        app._record_history_points(master)

        scenario_info = {str(s.get("leaderboardId", "")): {
            "name": s.get("scenarioName", ""),
            "entries": s.get("counts", {}).get("entries", ""),
        } for s in master}

        app._scenario_info = scenario_info
        
        app._jwt_token = None
        if password:
            app._update_status("Logging in to KovaaKs…")
            try:
                app._jwt_token = kovaaks_login(username, password)
            except Exception as e:
                logger.warning("Login failed, skipping score fetch: %s", e)
                app._update_status("Login failed — showing scenario list only.")

        scores_data = scores_cache.get("scores", {})
        user_by_lid = {}
        friends_by_lid = {}
        
        for lid, cached in scores_data.items():
            if lid in scenario_info:
                if "user" in cached:
                    user_by_lid[lid] = cached["user"]
                if "friends" in cached and cached["friends"]:
                    friends_by_lid[lid] = cached["friends"]

        if not app._jwt_token:
            app._user_by_lid = user_by_lid
            app._friends_by_lid = friends_by_lid
            app.run_in_gui_thread(app._rebuild_data)
            app._update_status(f"Done (Scenario list updated) — {len(master)} scenarios.")
            app._update_progress(100, 100)
            return

        all_lids = list(scenario_info.keys())
        work_items = [lid for lid in all_lids if lid not in scores_data]
        total_all = len(all_lids)
        total_to_fetch = len(work_items)
        cached_count = total_all - total_to_fetch
        
        app._user_by_lid = user_by_lid
        app._friends_by_lid = friends_by_lid

        if total_to_fetch == 0:
            app.run_in_gui_thread(app._rebuild_data_and_finish)
            return

        app._update_status(f"Fetching scores for {total_to_fetch} scenarios ({cached_count} cached)…")
        app.run_in_gui_thread(app._rebuild_data)

        lock = threading.Lock()
        errors = 0
        completed = 0
        session_expired = False
        start_time = time.time()
        last_refresh = [0]
        last_save = [0]
        eta_window = []

        def _save_cache():
            unified = {
                "scenarios": scores_cache.get("scenarios", []),
                "scores": scores_data,
                "entry_history": scores_cache.get("entry_history", {}),
            }
            save_scores_cache(unified)

        def _fetch_one(lid, session):
            nonlocal errors, completed, session_expired
            if session_expired:
                return

            try:
                data = kovaaks_get_friends_scores(app._jwt_token, lid, session=session)
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

            user_entry, friend_entries = parse_leaderboard_entries(data, username)

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

                if done - last_save[0] >= 200 or done == total_to_fetch:
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
                app._update_status(
                    f"Fetching scores… {done}/{total_to_fetch} "
                    f"({cached_count} cached, {errors} errors) — ETA {eta}"
                )
                prog = 0.2 + (0.8 * (done / total_to_fetch))
                app._update_progress(min(1.0, prog), 1.0)
                
            if done - last_refresh[0] >= 100:
                last_refresh[0] = done
                app.run_in_gui_thread(app._rebuild_data)

        session = requests.Session()
        with concurrent.futures.ThreadPoolExecutor(max_workers=25) as executor:
            executor.map(lambda lid: _fetch_one(lid, session), work_items)

        if session_expired:
            with lock:
                _save_cache()
            app._update_status("Session expired — progress saved. Try again.")
            app._jwt_token = None
            return

        with lock:
            _save_cache()
            
        # Finish up with a final rebuild
        app.run_in_gui_thread(lambda: app._rebuild_data_and_finish(errors))

    except Exception as e:
        logger.exception("Error in fetch thread")
        app._update_status(f"Error: {e}")
    finally:
        app.run_in_gui_thread(lambda: app._set_running(False))
