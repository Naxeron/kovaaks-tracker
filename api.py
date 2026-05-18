"""
KovaaKs Scenario Tracker — API helpers.

Shared HTTP request logic, authentication, and KovaaKs API wrappers.
Used by both the GUI application and the fetch_scenarios script.
"""

import base64
import datetime
import logging
import time
import concurrent.futures

import requests

logger = logging.getLogger("kovaaks")

# ---------------------------------------------------------------------------
# Common headers
# ---------------------------------------------------------------------------
KOVAAKS_HEADERS = {
    "Origin": "https://kovaaks.com",
    "Referer": "https://kovaaks.com/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------

def api_request_with_retry(method, url, timeout=30, max_retries=999,
                           session=None, **kwargs):
    """Make an HTTP request with retry on timeouts/5xx and exponential backoff.

    Handles:
    - 5xx server errors  → retry with backoff
    - Connection / timeout errors → retry with backoff
    - 429 (rate-limit) → retry with backoff
    - Other 4xx client errors → raise immediately (no retry)
    """
    req_func = (getattr(session, method.lower()) if session
                else getattr(requests, method.lower()))

    for attempt in range(max_retries + 1):
        try:
            resp = req_func(url, timeout=timeout, **kwargs)
            if resp.status_code < 500 or attempt == max_retries:
                resp.raise_for_status()
                if attempt > 0:
                    logger.info("Recovered %s %s after %d retries",
                                method.upper(), url, attempt)
                return resp
        except requests.exceptions.RequestException as e:
            # Don't retry 4xx client errors (except 429 rate-limit)
            if hasattr(e, "response") and e.response is not None:
                if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                    raise
            if attempt == max_retries:
                raise
            logger.warning("Connection error %s, retrying %d/%d...",
                           e, attempt + 1, max_retries)

        wait = min(60, 2 ** (attempt + 1))
        logger.warning("Server error/timeout, retrying in %ds…", wait)
        time.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# KovaaKs-specific API functions
# ---------------------------------------------------------------------------

def get_accurate_entry_count(leaderboard_id, session=None):
    """Fetch the accurate 'total' entries from the global leaderboard endpoint."""
    url = "https://kovaaks.com/webapp-backend/leaderboard/scores/global"
    params = {"leaderboardId": leaderboard_id, "page": 0, "max": 1}
    try:
        resp = api_request_with_retry(
            "get", url, params=params, timeout=15,
            max_retries=10, session=session)
        if resp:
            data = resp.json()
            return int(data.get("total", 0))
    except Exception as e:
        logger.debug("Failed to fetch accurate count for lid=%s: %s",
                     leaderboard_id, e)
    return None


def kovaaks_login(username, password):
    """Login to KovaaKs webapp, return JWT token string."""
    logger.debug("Logging in to KovaaKs as '%s'", username)
    url = "https://kovaaks.com/auth/webapp/login"
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {**KOVAAKS_HEADERS, "Authorization": f"Basic {credentials}"}
    resp = api_request_with_retry("post", url, headers=headers, data="", timeout=15)

    data = resp.json()
    auth = data.get("auth", {})
    logger.debug("Login auth keys: %s",
                 list(auth.keys()) if isinstance(auth, dict) else type(auth))

    if isinstance(auth, dict):
        for key in ("jwt", "token", "access_token", "firebaseJWT"):
            token = auth.get(key)
            if token and isinstance(token, str) and token.startswith("eyJ"):
                logger.info("Login successful (token from auth.%s, len=%d)",
                            key, len(token))
                return token

    raise ValueError(
        f"Could not find JWT in login response. Keys: {list(data.keys())}")


def kovaaks_get_friends_scores(token, leaderboard_id, session=None):
    """Fetch friends' scores for a given leaderboard ID."""
    url = "https://kovaaks.com/webapp-backend/leaderboard/scores/friends"
    headers = {**KOVAAKS_HEADERS, "Authorization": f"Bearer {token}"}
    resp = api_request_with_retry("get", url, params={
        "leaderboardId": leaderboard_id,
        "page": 0,
        "max": 50,
    }, headers=headers, timeout=30, session=session)
    return resp.json().get("data", [])


def fetch_all_scenarios(min_entries=0, session=None, progress_callback=None):
    """Fetch scenarios from the KovaaKs API (paginated, sorted by popularity).
    Stops early when all items on a page fall below *min_entries*.
    """
    from data_processing import get_estimated_fetch_count

    url = "https://kovaaks.com/webapp-backend/scenario/popular"
    all_data = []
    page = 0

    # Estimate total to provide better progress/ETA
    est_total = get_estimated_fetch_count(min_entries)
    start_time = time.time()

    # Single executor for the entire fetch (perf fix: was per-page before)
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        while True:
            logger.debug("Fetching all scenarios page %d", page)
            params = {"page": page, "max": 100}
            resp = api_request_with_retry("get", url, params=params, session=session)
            data = resp.json()
            items = data.get("data", [])
            if not items:
                break

            # Fetch accurate entry counts in parallel for the current page
            future_to_item = {
                executor.submit(get_accurate_entry_count,
                                it.get("leaderboardId"), session): it
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

            # Report progress
            if progress_callback:
                done = len(all_data)
                elapsed = time.time() - start_time
                if done > 0:
                    rate = done / elapsed
                    rem_est = max(0, est_total - done)
                    eta_s = rem_est / rate
                    mins, secs = divmod(int(eta_s), 60)
                    eta_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
                    status_msg = (f"Fetching scenarios… {done}/{int(est_total)}"
                                  f" — ETA {eta_str}")
                    progress_callback(done, est_total, status_msg)

            # Early stop: API returns by descending popularity
            if min_entries > 0:
                max_on_page = max(
                    (int(it.get("counts", {}).get("entries", 0)) for it in items),
                    default=0,
                )
                if max_on_page < min_entries:
                    logger.info("Stopping at page %d — max entries %d < %d",
                                page, max_on_page, min_entries)
                    break

            total = data.get("total", 0)
            page += 1
            if len(all_data) >= total:
                break
            time.sleep(0.1)

    logger.info("Fetched %d total scenarios with accurate counts", len(all_data))
    return all_data
