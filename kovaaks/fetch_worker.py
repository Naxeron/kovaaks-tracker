import time
import json
import gzip
import io
import logging
import threading
import concurrent.futures
from dataclasses import dataclass
import requests
from copy import deepcopy

from .constants import GITHUB_DATA_BASE, MIN_ENTRIES
from .api import (
    API_FETCH_WORKERS,
    RequestCancelled,
    api_request_with_retry,
    fetch_all_scenarios,
    kovaaks_login,
    kovaaks_get_friends_scores,
)
from .cache import save_scores_cache
from .config_helpers import save_config
from .scoring import prune_entry_history
from .data_processing import (
    get_estimated_fetch_count,
    get_estimated_matching_count,
    parse_leaderboard_entries,
)

logger = logging.getLogger("kovaaks")

DATASET_UNCHANGED = object()


@dataclass
class _DatasetCacheEntry:
    """Keep validators paired with the response that was actually applied."""

    etag: str = ""
    last_modified: str = ""
    scenario_data: object = None
    applied_cache: object = None
    applied_history: object = None


def mark_dataset_applied(app, filename):
    """Enable conditional downloads only after this response was installed.

    Entries live for this app session and share existing payload references. In
    particular, no second copy of the expanded history is retained. Persisted
    config validators alone cannot establish which data was successfully merged.
    """
    downloads = getattr(app, "_dataset_download_cache", None)
    scores_cache = getattr(app, "_scores_cache", None)
    if not isinstance(downloads, dict) or not isinstance(scores_cache, dict):
        return
    entry = downloads.get(filename)
    if not isinstance(entry, _DatasetCacheEntry):
        return
    if filename == "scenarios.json.gz":
        if scores_cache.get("scenarios") is not entry.scenario_data:
            return
    elif filename == "scenarios_history.json.gz":
        history = scores_cache.get("entry_history")
        if not isinstance(history, dict):
            return
        entry.applied_history = history
    else:
        return
    entry.applied_cache = scores_cache


def _applied_dataset_entry(app, filename):
    """Return a verified response only while its installed data is still active."""
    downloads = getattr(app, "_dataset_download_cache", None)
    scores_cache = getattr(app, "_scores_cache", None)
    if not isinstance(downloads, dict) or not isinstance(scores_cache, dict):
        return None
    entry = downloads.get(filename)
    if not isinstance(entry, _DatasetCacheEntry) or entry.applied_cache is not scores_cache:
        return None
    if filename == "scenarios.json.gz":
        return entry if scores_cache.get("scenarios") is entry.scenario_data else None
    return entry if scores_cache.get("entry_history") is entry.applied_history else None


def fetch_gzip_json_from_github(filename, app):
    """Read a rolling release asset, or report an applied response as unchanged."""
    if filename not in ("scenarios.json.gz", "scenarios_history.json.gz"):
        logger.warning("Unsupported dataset filename: %s", filename)
        return None
    url = f"{GITHUB_DATA_BASE}/{filename}"
    for attempt in range(3):
        try:
            cached = _applied_dataset_entry(app, filename)
            headers = {}
            if cached:
                if cached.etag:
                    headers["If-None-Match"] = cached.etag
                elif cached.last_modified:
                    headers["If-Modified-Since"] = cached.last_modified
            request_options = {"headers": headers} if headers else {}
            resp = api_request_with_retry("get", url, timeout=30, **request_options)
            if resp is None:
                return None
            if resp.status_code == 304:
                if headers and _applied_dataset_entry(app, filename) is cached:
                    return DATASET_UNCHANGED
                # A cache may have been replaced during the request. Never
                # accept an empty 304 response without its matching payload.
                if attempt < 2:
                    continue
                raise ValueError("Dataset returned 304 without matching cached data")
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
            downloads = getattr(app, "_dataset_download_cache", None)
            if not isinstance(downloads, dict):
                downloads = app._dataset_download_cache = {}
            # Do not acknowledge application here: cancellation or a failed
            # merge must leave the next request unconditional.
            downloads[filename] = _DatasetCacheEntry(
                etag=resp.headers.get("ETag", ""),
                last_modified=resp.headers.get("Last-Modified", ""),
                scenario_data=data if filename == "scenarios.json.gz" else None,
            )
            etag = resp.headers.get("ETag") or resp.headers.get("Last-Modified")
            if etag:
                last_etags = app._cfg.setdefault("last_etags", {})
                metadata_changed = last_etags.get(filename) != etag
                last_etags[filename] = etag
                # Keep a legacy plaintext config intact until its credentials
                # have been migrated successfully to the OS credential store.
                if metadata_changed and not getattr(app, "_legacy_migration_pending", False):
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
    data_lock = getattr(app, "_data_lock", threading.RLock())
    cache_changed = False
    completion = None

    def finish(cancelled_run=False, *args, **kwargs):
        """Notify the browser after persistence and fetch-flag finalization."""
        nonlocal completion
        method = app._rebuild_data_and_cancelled if cancelled_run else app._rebuild_data_and_finish
        completion = (method, args, kwargs)

    def persist_changes(wait=False):
        """Queue only changed data; never compress inside the score-worker lock."""
        nonlocal cache_changed
        queue = getattr(type(app), "_queue_cache_save", None)
        if cache_changed:
            cache_changed = False
            if queue is not None:
                queue(app, wait=False)
            else:
                # Headless callers can use this worker without the GUI save queue.
                with data_lock:
                    snapshot = deepcopy(app._scores_cache)
                save_scores_cache(snapshot)
        if wait:
            flush = getattr(type(app), "_flush_cache_saves", None)
            if flush is not None:
                flush(app)

    def _is_cancelled():
        """Keep cancellation sticky for this run after the shared flag resets."""
        if getattr(app, "_fetch_cancelled", False) is True:
            cancelled.set()
        return cancelled.is_set()

    try:
        if _is_cancelled():
            finish(True, silent=silent)
            return

        app._update_progress(0.0, 1.0)
        app._update_status("Fetching all scenarios…")
        scores_cache = app._scores_cache
        min_entries_threshold = int(app._cfg.get("min_entries", MIN_ENTRIES))
        
        app._update_progress(0.01, 1.0)
        all_scenarios = fetch_gzip_json_from_github("scenarios.json.gz", app)
        if all_scenarios is DATASET_UNCHANGED:
            all_scenarios = scores_cache.get("scenarios", [])
        if _is_cancelled():
            finish(True, silent=silent)
            return

        app._update_progress(0.03, 1.0)
        ext_history = fetch_gzip_json_from_github("scenarios_history.json.gz", app)
        app._update_progress(0.05, 1.0)

        if ext_history is not DATASET_UNCHANGED and ext_history:
            h_ts, h_data = ext_history.get("timestamps", []), ext_history.get("history", {})
            if h_ts and h_data:
                with data_lock:
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
                    cache_changed = merged_count > 0 or cache_changed
                    cache_changed = prune_entry_history(local_history) or cache_changed
                    mark_dataset_applied(app, "scenarios_history.json.gz")

        app._update_progress(0.10, 1.0)

        if not all_scenarios:
            if _is_cancelled():
                finish(True, silent=silent)
                return
            app._update_status("Fetching scenarios (API fallback)…")
            total_est = get_estimated_fetch_count(min_entries_threshold) + get_estimated_matching_count(min_entries_threshold)
            def check_cancel():
                if _is_cancelled():
                    raise RequestCancelled("Fetch cancelled")

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
                    finish(True, silent=silent)
                    return
                raise
            logger.info("API returned %d total scenarios", len(all_scenarios))
            app._update_progress(0.10, 1.0)

        if _is_cancelled():
            finish(True, silent=silent)
            return
        if not all_scenarios:
            raise RuntimeError("No scenarios available; keeping the previous cache")
        with data_lock:
            cache_changed = scores_cache.get("scenarios") != all_scenarios or cache_changed
            scores_cache["scenarios"] = all_scenarios
            app._cache_corrupted = False
            mark_dataset_applied(app, "scenarios.json.gz")
        app._update_progress(0.12, 1.0)
        app._update_progress(0.15, 1.0)

        master = [s for s in all_scenarios if int(s.get("counts", {}).get("entries", 0) or 0) >= min_entries_threshold]
        with data_lock:
            # Record and bound history before the first durable checkpoint.
            app._record_history_points(master)
            cache_changed = scores_cache.pop("_dirty", False) or cache_changed
        app._update_progress(0.22, 1.0)

        scenario_info = {str(s.get("leaderboardId", "")): {
            "name": s.get("scenarioName", ""),
            "entries": s.get("counts", {}).get("entries", ""),
        } for s in master}
        with data_lock:
            app._scenario_info = scenario_info
        
        if _is_cancelled():
            finish(True, silent=silent)
            return

        app._jwt_token = None
        if password:
            app._update_progress(0.22, 1.0)
            app._update_status("Logging in to KovaaKs…")
            try:
                app._jwt_token = kovaaks_login(username, password, cancel_check=_is_cancelled)
                if _is_cancelled():
                    finish(True, silent=silent)
                    return
                app._update_progress(0.25, 1.0)
            except Exception as e:
                if _is_cancelled():
                    finish(True, silent=silent)
                    return
                logger.warning("Login failed, skipping score fetch: %s", e)
                app._update_status("Login failed — showing scenario list only.")
                app._update_progress(0.25, 1.0)

        scores_data = scores_cache.get("scores", {})
        user_by_lid = {k: v["user"] for k, v in scores_data.items() if k in scenario_info and "user" in v}
        friends_by_lid = {k: v["friends"] for k, v in scores_data.items() if k in scenario_info and v.get("friends")}

        with data_lock:
            app._user_by_lid, app._friends_by_lid = user_by_lid, friends_by_lid

        if not app._jwt_token:
            app._rebuild_data()
            finish(False, silent=silent, msg=f"Done (Scenario list updated) — {len(master)} scenarios.")
            return

        all_lids = list(scenario_info.keys())
        name_to_lid = {info["name"]: lid for lid, info in scenario_info.items()}
        with data_lock:
            newly_played_names = scores_cache.pop("newly_played_scenarios", [])
            cache_changed = bool(newly_played_names) or cache_changed
        newly_played_lids = {name_to_lid[name] for name in newly_played_names if name in name_to_lid}
        
        local_stats_cache = scores_cache.get("local_stats", {})
        
        work_items = all_lids

        total_to_fetch = len(work_items)
        cached_count = len(all_lids) - total_to_fetch

        if total_to_fetch == 0:
            finish(False, silent=silent)
            return

        if _is_cancelled():
            finish(True, silent=silent)
            return

        app._update_status(f"Fetching scores for {total_to_fetch} scenarios ({cached_count} cached)…")
        app._rebuild_data()

        errors = completed = 0
        session_expired = False
        start_time = time.time()
        last_save_time = [time.monotonic()]
        eta_window = []

        def _fetch_one(lid, session):
            nonlocal errors, completed, session_expired, cache_changed
            if session_expired or _is_cancelled():
                return

            try:
                data = kovaaks_get_friends_scores(
                    app._jwt_token, lid, session=session, cancel_check=_is_cancelled)
            except RequestCancelled:
                return
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
                with data_lock:
                    cache_entry = {}
                    if user_entry:
                        user_by_lid[lid] = cache_entry["user"] = user_entry
                    else:
                        user_by_lid.pop(lid, None)
                    if friend_entries:
                        friends_by_lid[lid] = cache_entry["friends"] = friend_entries
                    else:
                        friends_by_lid.pop(lid, None)
                    cache_changed = scores_data.get(lid) != cache_entry or cache_changed
                    scores_data[lid] = cache_entry
                    scores_cache["scores"] = scores_data
                completed += 1
                done = completed

                # Checkpoint by elapsed time, not every small batch of responses.
                # The shared writer compresses outside both worker and data locks.
                if time.monotonic() - last_save_time[0] >= 30:
                    last_save_time[0] = time.monotonic()
                    persist_changes()

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

        session = requests.Session()
        session.mount("https://", requests.adapters.HTTPAdapter(
            pool_connections=API_FETCH_WORKERS, pool_maxsize=API_FETCH_WORKERS))
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=API_FETCH_WORKERS)
        try:
            futures = [executor.submit(_fetch_one, lid, session) for lid in work_items]
            for future in concurrent.futures.as_completed(futures):
                if _is_cancelled():
                    break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if _is_cancelled():
            finish(True, silent=silent)
            return

        if session_expired:
            app._jwt_token = None
            finish(False, silent=silent, msg="Session expired — progress saved. Try again.")
            return

        finish(False, errors, silent=silent)

    except Exception as e:
        logger.exception("Error in fetch thread")
        app._update_status(f"Error: {e}")
    finally:
        # In-flight requests may outlive this run because shutdown is nonblocking.
        with lock:
            cancelled.set()
        # All accepted mutations are now stable; flush the final checkpoint once.
        try:
            persist_changes(wait=True)
        finally:
            app._fetch_cancelled = False
            app._fetch_in_progress = False
        if completion is not None:
            method, args, kwargs = completion
            method(*args, **kwargs)
