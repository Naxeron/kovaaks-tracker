import time
import json
import gzip
import io
import logging
import threading
import concurrent.futures
import requests

from .constants import GITHUB_RAW_BASE, MIN_ENTRIES
from .api import (
    api_request_with_retry,
    fetch_all_scenarios,
    kovaaks_login,
    kovaaks_get_friends_scores,
)
from .cache import save_scores_cache
from .config_helpers import save_config
from .data_processing import (
    get_estimated_fetch_count,
    get_estimated_matching_count,
    parse_leaderboard_entries,
)

logger = logging.getLogger("kovaaks")

def fetch_gzip_json_from_github(filename, app):
    url = f"{GITHUB_RAW_BASE}/{filename}"
    try:
        resp = api_request_with_retry("get", url, timeout=30)
        if resp.status_code == 200:
            etag = resp.headers.get("ETag") or resp.headers.get("Last-Modified")
            if etag:
                app._cfg.setdefault("last_etags", {})[filename] = etag
                save_config(app._cfg)
            with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Failed to fetch %s from GitHub: %s", filename, e)
    return None


def run_fetch_all(app, username, password):
    """Background worker that fetches all scenarios and updates the GUI state."""
    try:
        app._update_status("Fetching all scenarios…")
        scores_cache = app._scores_cache
        min_entries_threshold = int(app._cfg.get("min_entries", MIN_ENTRIES))
        
        all_scenarios = fetch_gzip_json_from_github("scenarios.json.gz", app)
        ext_history = fetch_gzip_json_from_github("scenarios_history.json.gz", app)

        if ext_history:
            h_ts, h_data = ext_history.get("timestamps", []), ext_history.get("history", {})
            if h_ts and h_data:
                local_history = scores_cache.setdefault("entry_history", {})
                merged_count = 0
                for lid, counts in h_data.items():
                    lid_hist = local_history.setdefault(str(lid), {})
                    for i, count in enumerate(counts):
                        if count is not None and i < len(h_ts) and h_ts[i] not in lid_hist:
                            lid_hist[h_ts[i]] = count
                            merged_count += 1
                logger.info("Merged %d history points from GitHub", merged_count)

        if not all_scenarios:
            app._update_status("Fetching scenarios (API fallback)…")
            total_est = get_estimated_fetch_count(min_entries_threshold) + get_estimated_matching_count(min_entries_threshold)
            cb = lambda done, tot, msg: (app._update_status(msg), app._update_progress(min(0.95, done / total_est if total_est > 0 else 0), 1.0))
            all_scenarios = fetch_all_scenarios(min_entries=min_entries_threshold, session=requests.Session(), progress_callback=cb)
            logger.info("API returned %d total scenarios", len(all_scenarios))
        else:
            app._update_progress(0.4, 1.0)

        scores_cache["scenarios"] = all_scenarios
        save_scores_cache(scores_cache)

        master = [s for s in all_scenarios if int(s.get("counts", {}).get("entries", 0) or 0) >= min_entries_threshold]
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
        user_by_lid = {k: v["user"] for k, v in scores_data.items() if k in scenario_info and "user" in v}
        friends_by_lid = {k: v["friends"] for k, v in scores_data.items() if k in scenario_info and v.get("friends")}

        app._user_by_lid, app._friends_by_lid = user_by_lid, friends_by_lid

        if not app._jwt_token:
            app.run_in_gui_thread(app._rebuild_data)
            app._update_status(f"Done (Scenario list updated) — {len(master)} scenarios.")
            app._update_progress(100, 100)
            return

        all_lids = list(scenario_info.keys())
        work_items = [lid for lid in all_lids if lid not in scores_data]
        total_to_fetch = len(work_items)
        cached_count = len(all_lids) - total_to_fetch

        if total_to_fetch == 0:
            app.run_in_gui_thread(app._rebuild_data_and_finish)
            return

        app._update_status(f"Fetching scores for {total_to_fetch} scenarios ({cached_count} cached)…")
        app.run_in_gui_thread(app._rebuild_data)

        lock = threading.Lock()
        errors = completed = 0
        session_expired = False
        start_time = time.time()
        last_refresh = [0]
        last_save = [0]
        eta_window = []

        def _save_cache():
            save_scores_cache({
                "scenarios": scores_cache.get("scenarios", []),
                "scores": scores_data,
                "entry_history": scores_cache.get("entry_history", {}),
            })

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
                with lock: errors += 1
                return
            except Exception:
                with lock: errors += 1
                return

            if data is None:
                return

            user_entry, friend_entries = parse_leaderboard_entries(data, username)
            with lock:
                cache_entry = {}
                if user_entry:
                    user_by_lid[lid] = cache_entry["user"] = user_entry
                if friend_entries:
                    friends_by_lid[lid] = cache_entry["friends"] = friend_entries
                scores_data[lid] = cache_entry
                completed += 1
                done = completed

                if done - last_save[0] >= 200 or done == total_to_fetch:
                    last_save[0] = done
                    _save_cache()

            if done % 20 == 0 or done == total_to_fetch:
                now = time.time()
                eta_window.append((done, now))
                if len(eta_window) > 10: eta_window.pop(0)
                rate = (done - eta_window[0][0]) / (now - eta_window[0][1]) if len(eta_window) >= 2 and now > eta_window[0][1] else (done / (now - start_time) if now > start_time else 0)
                rem = (total_to_fetch - done) / rate if rate > 0 else 0
                m, s = divmod(int(rem), 60)
                app._update_status(f"Fetching scores… {done}/{total_to_fetch} ({cached_count} cached, {errors} errors) — ETA {f'{m}m{s:02d}s' if m else f'{s}s'}")
                app._update_progress(min(1.0, 0.2 + 0.8 * (done / total_to_fetch)), 1.0)
                
            if done - last_refresh[0] >= 100:
                last_refresh[0] = done
                app.run_in_gui_thread(app._rebuild_data)

        session = requests.Session()
        with concurrent.futures.ThreadPoolExecutor(max_workers=25) as executor:
            executor.map(lambda lid: _fetch_one(lid, session), work_items)

        if session_expired:
            with lock: _save_cache()
            app._update_status("Session expired — progress saved. Try again.")
            app._jwt_token = None
            return

        with lock: _save_cache()
        app.run_in_gui_thread(lambda: app._rebuild_data_and_finish(errors))

    except Exception as e:
        logger.exception("Error in fetch thread")
        app._update_status(f"Error: {e}")
    finally:
        app.run_in_gui_thread(lambda: app._set_running(False))

