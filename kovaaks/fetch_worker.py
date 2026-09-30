import time
import json
import gzip
import io
import logging
import threading
import concurrent.futures
import requests

from .constants import GITHUB_DATA_BASE, MIN_ENTRIES
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
    """Read a rolling release asset, briefly retrying asset replacement gaps."""
    if filename not in ("scenarios.json.gz", "scenarios_history.json.gz"):
        logger.warning("Unsupported dataset filename: %s", filename)
        return None
    url = f"{GITHUB_DATA_BASE}/{filename}"
    for attempt in range(3):
        try:
            resp = api_request_with_retry("get", url, timeout=30)
            if resp is None:
                return None
            if resp.status_code != 200:
                resp.raise_for_status()
                return None
            with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as f:
                data = json.load(f)
            if filename == "scenarios.json.gz":
                if not isinstance(data, list) or any(
                    not isinstance(item, dict) or not item.get("leaderboardId")
                    or not isinstance(item.get("counts"), dict)
                    for item in data
                ):
                    raise ValueError("Expected a scenario list")
            elif not (isinstance(data, dict) and isinstance(data.get("timestamps"), list)
                      and all(isinstance(stamp, str) for stamp in data["timestamps"])
                      and isinstance(data.get("history"), dict)
                      and all(isinstance(counts, list) and len(counts) == len(data["timestamps"])
                              for counts in data["history"].values())):
                raise ValueError("Expected timestamps and history in the dataset")
            etag = resp.headers.get("ETag") or resp.headers.get("Last-Modified")
            if etag:
                app._cfg.setdefault("last_etags", {})[filename] = etag
                # Keep a legacy plaintext config intact until its credentials
                # have been migrated successfully to the OS credential store.
                if not getattr(app, "_legacy_migration_pending", False):
                    save_config(app._cfg)
            return data
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404 and attempt < 2:
                # Replacing a release asset briefly deletes its previous URL.
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.warning("Failed to fetch %s from GitHub: %s", filename, e)
            break
        except Exception as e:
            logger.warning("Failed to fetch %s from GitHub: %s", filename, e)
            break
    return None


def run_fetch_all(app, username, password, silent=False):
    """Background worker that fetches all scenarios and updates the GUI state."""
    app._fetch_in_progress = True
    cancelled = threading.Event()
    lock = threading.Lock()

    def _is_cancelled():
        """Keep cancellation sticky for this run after the shared flag resets."""
        if getattr(app, "_fetch_cancelled", False) is True:
            cancelled.set()
        return cancelled.is_set()

    try:
        if _is_cancelled():
            app._rebuild_data_and_cancelled(silent=silent)
            return

        app._update_progress(0.0, 1.0)
        app._update_status("Fetching all scenarios…")
        scores_cache = app._scores_cache
        min_entries_threshold = int(app._cfg.get("min_entries", MIN_ENTRIES))
        
        app._update_progress(0.01, 1.0)
        all_scenarios = fetch_gzip_json_from_github("scenarios.json.gz", app)
        if _is_cancelled():
            app._rebuild_data_and_cancelled(silent=silent)
            return

        app._update_progress(0.03, 1.0)
        ext_history = fetch_gzip_json_from_github("scenarios_history.json.gz", app)
        app._update_progress(0.05, 1.0)

        if ext_history:
            h_ts, h_data = ext_history.get("timestamps", []), ext_history.get("history", {})
            if h_ts and h_data:
                local_history = scores_cache.setdefault("entry_history", {})
                merged_count = 0
                total_items = len(h_data)
                for idx, (lid, counts) in enumerate(h_data.items()):
                    lid_hist = local_history.setdefault(str(lid), {})
                    for i, count in enumerate(counts):
                        if count is not None and i < len(h_ts) and h_ts[i] not in lid_hist:
                            lid_hist[h_ts[i]] = count
                            merged_count += 1
                    if idx % 1000 == 0:
                        progress = 0.05 + 0.05 * (idx / total_items if total_items > 0 else 0)
                        app._update_progress(progress, 1.0)
                logger.info("Merged %d history points from GitHub", merged_count)

        app._update_progress(0.10, 1.0)

        if not all_scenarios:
            if _is_cancelled():
                app._rebuild_data_and_cancelled(silent=silent)
                return
            app._update_status("Fetching scenarios (API fallback)…")
            total_est = get_estimated_fetch_count(min_entries_threshold) + get_estimated_matching_count(min_entries_threshold)
            def check_cancel():
                if _is_cancelled():
                    raise RuntimeError("Fetch cancelled")

            def cb(done, tot, msg):
                check_cancel()
                app._update_status(msg)
                app._update_progress(0.01 + 0.09 * min(1.0, done / total_est if total_est > 0 else 0), 1.0)
            try:
                all_scenarios = fetch_all_scenarios(
                    min_entries=min_entries_threshold, 
                    session=requests.Session(), 
                    progress_callback=cb,
                    cancel_check=check_cancel
                )
            except RuntimeError as re:
                if str(re) == "Fetch cancelled":
                    app._rebuild_data_and_cancelled(silent=silent)
                    return
                raise
            logger.info("API returned %d total scenarios", len(all_scenarios))
            app._update_progress(0.10, 1.0)

        if _is_cancelled():
            app._rebuild_data_and_cancelled(silent=silent)
            return
        if not all_scenarios:
            raise RuntimeError("No scenarios available; keeping the previous cache")
        scores_cache["scenarios"] = all_scenarios
        app._cache_corrupted = False
        app._update_progress(0.12, 1.0)
        save_scores_cache(scores_cache)
        app._update_progress(0.15, 1.0)

        master = [s for s in all_scenarios if int(s.get("counts", {}).get("entries", 0) or 0) >= min_entries_threshold]
        app._record_history_points(master)
        app._update_progress(0.22, 1.0)

        scenario_info = {str(s.get("leaderboardId", "")): {
            "name": s.get("scenarioName", ""),
            "entries": s.get("counts", {}).get("entries", ""),
        } for s in master}
        app._scenario_info = scenario_info
        
        if _is_cancelled():
            app._rebuild_data_and_cancelled(silent=silent)
            return

        app._jwt_token = None
        if password:
            app._update_progress(0.22, 1.0)
            app._update_status("Logging in to KovaaKs…")
            try:
                app._jwt_token = kovaaks_login(username, password)
                if _is_cancelled():
                    app._rebuild_data_and_cancelled(silent=silent)
                    return
                app._update_progress(0.25, 1.0)
            except Exception as e:
                if _is_cancelled():
                    app._rebuild_data_and_cancelled(silent=silent)
                    return
                logger.warning("Login failed, skipping score fetch: %s", e)
                app._update_status("Login failed — showing scenario list only.")
                app._update_progress(0.25, 1.0)

        scores_data = scores_cache.get("scores", {})
        user_by_lid = {k: v["user"] for k, v in scores_data.items() if k in scenario_info and "user" in v}
        friends_by_lid = {k: v["friends"] for k, v in scores_data.items() if k in scenario_info and v.get("friends")}

        app._user_by_lid, app._friends_by_lid = user_by_lid, friends_by_lid

        if not app._jwt_token:
            app._rebuild_data()
            app._rebuild_data_and_finish(silent=silent, msg=f"Done (Scenario list updated) — {len(master)} scenarios.")
            return

        all_lids = list(scenario_info.keys())
        name_to_lid = {info["name"]: lid for lid, info in scenario_info.items()}
        newly_played_names = scores_cache.pop("newly_played_scenarios", [])
        newly_played_lids = {name_to_lid[name] for name in newly_played_names if name in name_to_lid}
        
        local_stats_cache = scores_cache.get("local_stats", {})
        
        work_items = all_lids

        total_to_fetch = len(work_items)
        cached_count = len(all_lids) - total_to_fetch

        if total_to_fetch == 0:
            app._rebuild_data_and_finish(silent=silent)
            return

        if _is_cancelled():
            app._rebuild_data_and_cancelled(silent=silent)
            return

        app._update_status(f"Fetching scores for {total_to_fetch} scenarios ({cached_count} cached)…")
        app._rebuild_data()

        errors = completed = 0
        session_expired = False
        start_time = time.time()
        last_refresh = [0]
        last_save = [0]
        eta_window = []

        def _save_cache():
            scores_cache["scores"] = scores_data
            save_scores_cache(scores_cache)

        def _fetch_one(lid, session):
            nonlocal errors, completed, session_expired
            if session_expired or _is_cancelled():
                return

            try:
                data = kovaaks_get_friends_scores(app._jwt_token, lid, session=session)
            except requests.exceptions.HTTPError as e:
                if _is_cancelled():
                    return
                if e.response is not None and e.response.status_code == 401:
                    session_expired = True
                    return
                with lock: errors += 1
                return
            except Exception:
                if _is_cancelled():
                    return
                with lock: errors += 1
                return

            if data is None or _is_cancelled():
                return

            user_entry, friend_entries = parse_leaderboard_entries(data, username)
            with lock:
                if _is_cancelled():
                    return
                cache_entry = {}
                if user_entry:
                    user_by_lid[lid] = cache_entry["user"] = user_entry
                else:
                    user_by_lid.pop(lid, None)
                if friend_entries:
                    friends_by_lid[lid] = cache_entry["friends"] = friend_entries
                else:
                    friends_by_lid.pop(lid, None)
                scores_data[lid] = cache_entry
                completed += 1
                done = completed

                if done - last_save[0] >= 200 or done == total_to_fetch:
                    last_save[0] = done
                    _save_cache()

            # Serialize callbacks with finalization so a checked callback cannot
            # resume after this run releases the app for the next fetch.
            with lock:
                if (done % 20 == 0 or done == total_to_fetch) and not _is_cancelled():
                    now = time.time()
                    eta_window.append((done, now))
                    if len(eta_window) > 10: eta_window.pop(0)
                    rate = (done - eta_window[0][0]) / (now - eta_window[0][1]) if len(eta_window) >= 2 and now > eta_window[0][1] else (done / (now - start_time) if now > start_time else 0)
                    rem = (total_to_fetch - done) / rate if rate > 0 else 0
                    m, s = divmod(int(rem), 60)
                    app._update_status(f"Fetching scores… {done}/{total_to_fetch} ({cached_count} cached, {errors} errors) — ETA {f'{m}m{s:02d}s' if m else f'{s}s'}")
                    if not _is_cancelled():
                        app._update_progress(min(1.0, 0.25 + 0.75 * (done / total_to_fetch)), 1.0)

                if done - last_refresh[0] >= 100 and not _is_cancelled():
                    last_refresh[0] = done
                    app._rebuild_data()

        session = requests.Session()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=25)
        try:
            futures = [executor.submit(_fetch_one, lid, session) for lid in work_items]
            for future in concurrent.futures.as_completed(futures):
                if _is_cancelled():
                    break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if _is_cancelled():
            with lock: _save_cache()
            app._rebuild_data_and_cancelled(silent=silent)
            return

        if session_expired:
            with lock: _save_cache()
            app._jwt_token = None
            app._rebuild_data_and_finish(silent=silent, msg="Session expired — progress saved. Try again.")
            return

        with lock: _save_cache()
        app._rebuild_data_and_finish(errors, silent=silent)

    except Exception as e:
        logger.exception("Error in fetch thread")
        app._update_status(f"Error: {e}")
    finally:
        # In-flight requests may outlive this run because shutdown is nonblocking.
        with lock:
            cancelled.set()
        app._fetch_cancelled = False
        app._fetch_in_progress = False
