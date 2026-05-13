#!/usr/bin/env python3
import requests
import json
import time
import concurrent.futures
import logging
import os

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("fetch_scenarios")

SCENARIOS_JSON = "scenarios.json"

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

def fetch_all_scenarios():
    url = "https://kovaaks.com/webapp-backend/scenario/popular"
    all_data = []
    page = 0
    session = requests.Session()
    
    # We fetch a large number to ensure we cover what most users want.
    # ~18,000 scenarios in total. We'll fetch until the API gives no more data.
    # or until entry counts are very low. 
    # For GitHub Actions, we can afford to fetch most of them.
    
    MIN_ENTRIES_LIMIT = 10 # Only fetch scenarios with at least 10 entries to keep file size reasonable

    while True:
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
                    if "scenario" in item and "counts" in item["scenario"]:
                        item["scenario"]["counts"]["entries"] = accurate_count

        all_data.extend(items)
        
        # Check if we should stop
        max_on_page = max(
            (int(it.get("counts", {}).get("entries", 0)) for it in items),
            default=0,
        )
        if max_on_page < MIN_ENTRIES_LIMIT:
            logger.info(f"Stopping at page {page} - max entries {max_on_page} < {MIN_ENTRIES_LIMIT}")
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
    try:
        scenarios = fetch_all_scenarios()
        with open(SCENARIOS_JSON, "w", encoding="utf-8") as f:
            json.dump(scenarios, f, separators=(",", ":"))
        logger.info(f"Saved {len(scenarios)} scenarios to {SCENARIOS_JSON}")
    except Exception as e:
        logger.error(f"Error in fetch script: {e}")
        exit(1)
