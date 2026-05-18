#!/usr/bin/env python3
import requests
import json
import time
import concurrent.futures
import logging
import os
import gzip
import datetime

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("fetch_scenarios")

SCENARIOS_JSON = "scenarios.json.gz"
SCENARIOS_HISTORY_JSON = "scenarios_history.json.gz"

def _api_request_with_retry(method, url, timeout=30, max_retries=5, session=None, **kwargs):
    req_func = getattr(session, method.lower()) if session else getattr(requests, method.lower())
    for attempt in range(max_retries + 1):
        try:
            resp = req_func(url, timeout=timeout, **kwargs)
            if resp.status_code < 500 or attempt == max_retries:
                resp.raise_for_status()
                return resp
        except (requests.exceptions.RequestException, requests.exceptions.Timeout) as e:
            if attempt == max_retries:
                raise
            logger.warning(f"Connection error {e}, retrying {attempt + 1}/{max_retries}...")
        
        wait = min(60, 2 ** (attempt + 1))
        time.sleep(wait)
    return None

def _get_accurate_entry_count(leaderboard_id, session=None):
    url = "https://kovaaks.com/webapp-backend/leaderboard/scores/global"
    params = {"leaderboardId": leaderboard_id, "page": 0, "max": 1}
    try:
        resp = _api_request_with_retry("get", url, params=params, timeout=15, max_retries=3, session=session)
        if resp:
            data = resp.json()
            return int(data.get("total", 0))
    except Exception as e:
        logger.debug(f"Failed to fetch accurate count for lid={leaderboard_id}: {e}")
    return None

def fetch_all_scenarios(pages_limit=0, entries_limit=10):
    url = "https://kovaaks.com/webapp-backend/scenario/popular"
    all_data = []
    page = 0
    session = requests.Session()
    
    # Increase connection pool size to match max_workers in ThreadPoolExecutor
    adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    while True:
        if pages_limit > 0 and page >= pages_limit:
            logger.info(f"Reached page limit of {pages_limit}")
            break

        logger.info(f"Fetching page {page}")
        params = {"page": page, "max": 100}
        try:
            resp = _api_request_with_retry("get", url, params=params, session=session)
            data = resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch page {page}: {e}")
            break
            
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
                    if "counts" not in item:
                        item["counts"] = {}
                    item["counts"]["entries"] = accurate_count

        all_data.extend(items)
        
        # Check if we should stop
        max_on_page = max(
            (int(it.get("counts", {}).get("entries", 0)) for it in items),
            default=0,
        )
        if max_on_page < entries_limit:
            logger.info(f"Stopping at page {page} - max entries {max_on_page} < {entries_limit}")
            break

        total = data.get("total", 0)
        page += 1
        if len(all_data) >= total:
            break
        
        # Respectful delay
        time.sleep(0.2)

    logger.info(f"Fetched {len(all_data)} total scenarios")
    return all_data

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch KovaaKs scenarios and update cache.")
    parser.add_argument("--pages", type=int, default=0, help="Number of pages to fetch (0 for all)")
    parser.add_argument("--min-entries", type=int, default=1000, help="Stop fetching when max entries on a page falls below this")
    args = parser.parse_args()

    try:
        # Load existing scenarios if file exists
        existing_scenarios = {}
        if os.path.exists(SCENARIOS_JSON):
            try:
                with gzip.open(SCENARIOS_JSON, "rt", encoding="utf-8") as f:
                    data = json.load(f)
                    # Use leaderboardId as key for deduplication
                    existing_scenarios = {s.get("leaderboardId"): s for s in data if s.get("leaderboardId")}
                logger.info(f"Loaded {len(existing_scenarios)} existing scenarios from {SCENARIOS_JSON}")
            except Exception as e:
                logger.warning(f"Could not load existing scenarios: {e}")

        # Fetch new scenarios
        new_scenarios_list = fetch_all_scenarios(pages_limit=args.pages, entries_limit=args.min_entries)
        
        # Merge new into existing (new overwrites old for same leaderboardId)
        for s in new_scenarios_list:
            l_id = s.get("leaderboardId")
            if l_id:
                existing_scenarios[l_id] = s
        
        # Convert back to list and clean up
        merged_list = list(existing_scenarios.values())
        
        # Sort by entries count descending
        merged_list.sort(key=lambda x: int(x.get("counts", {}).get("entries", 0)), reverse=True)

        with gzip.open(SCENARIOS_JSON, "wt", encoding="utf-8") as f:
            json.dump(merged_list, f, separators=(",", ":"))
        
        logger.info(f"Merged and saved {len(merged_list)} total scenarios to {SCENARIOS_JSON}")

        # --- History Tracking ---
        history_data = {"timestamps": [], "history": {}}
        if os.path.exists(SCENARIOS_HISTORY_JSON):
            try:
                with gzip.open(SCENARIOS_HISTORY_JSON, "rt", encoding="utf-8") as f:
                    history_data = json.load(f)
                logger.info(f"Loaded history with {len(history_data.get('timestamps', []))} timestamps")
            except Exception as e:
                logger.warning(f"Could not load history: {e}")

        # Add current timestamp
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        now_str = now.isoformat()
        
        replace_latest = False
        if history_data["timestamps"]:
            last_ts_str = history_data["timestamps"][-1]
            try:
                # Handle potential Z suffix just in case
                lk = last_ts_str.replace("Z", "+00:00") if "Z" in last_ts_str else last_ts_str
                last_dt = datetime.datetime.fromisoformat(lk).replace(tzinfo=None)
                if (now - last_dt).total_seconds() < 3600:
                    replace_latest = True
            except ValueError:
                pass

        if not replace_latest:
            history_data["timestamps"].append(now_str)
            for lid in history_data["history"]:
                history_data["history"][lid].append(None)
        
        # Update history for all merged scenarios
        current_history = history_data["history"]
        for s in merged_list:
            lid = str(s["leaderboardId"])
            entries = s["counts"]["entries"]
            if lid not in current_history:
                # Initialize with nulls for past timestamps to keep alignment
                current_history[lid] = [None] * len(history_data["timestamps"])
            current_history[lid][-1] = entries

        # Ensure all existing LIDs also get a value (None if not in current fetch)
        for lid, counts in current_history.items():
            while len(counts) < len(history_data["timestamps"]):
                counts.append(None)

        # Prune to last 168 records (7 days if 1 point per hour)
        MAX_HISTORY = 168
        if len(history_data["timestamps"]) > MAX_HISTORY:
            history_data["timestamps"] = history_data["timestamps"][-MAX_HISTORY:]
            for lid in current_history:
                current_history[lid] = current_history[lid][-MAX_HISTORY:]

        # Save compressed history
        try:
            with gzip.open(SCENARIOS_HISTORY_JSON, "wt", encoding="utf-8") as f:
                json.dump(history_data, f, separators=(",", ":"))
            logger.info(f"Saved compressed history to {SCENARIOS_HISTORY_JSON}")
        except Exception as e:
            logger.error(f"Failed to save history: {e}")

    except Exception as e:
        logger.error(f"Error in fetch script: {e}")
        exit(1)
