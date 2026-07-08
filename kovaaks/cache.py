"""
KovaaKs Scenario Tracker — cache utilities.

Handles loading and saving the gzip-compressed JSON scores cache.
"""

import gzip
import json
import logging
import os
import threading

logger = logging.getLogger("kovaaks")

PROJECT_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCORES_CACHE = os.path.join(PROJECT_DIR, "data", "scores_cache.json.gz")

_cache_lock = threading.Lock()


def load_scores_cache():
    """Load the unified gzip JSON cache from disk. Returns empty dict on failure."""
    if not os.path.exists(SCORES_CACHE):
        return {}
    try:
        with gzip.open(SCORES_CACHE, "rt", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Loaded cache from %s", SCORES_CACHE)
        return data
    except (json.JSONDecodeError, OSError, EOFError) as e:
        logger.warning("Could not load cache: %s", e)
        return {}


def save_scores_cache(cache_dict):
    """Save the unified cache dict to gzip-compressed JSON atomically."""
    with _cache_lock:
        tmp_cache = SCORES_CACHE + ".tmp"
        try:
            os.makedirs(os.path.dirname(SCORES_CACHE), exist_ok=True)
            with gzip.open(tmp_cache, "wt", encoding="utf-8") as f:
                json.dump(cache_dict, f, separators=(",", ":"))
            os.replace(tmp_cache, SCORES_CACHE)
        except OSError as e:
            logger.warning("Could not save cache: %s", e)
            if os.path.exists(tmp_cache):
                try:
                    os.remove(tmp_cache)
                except OSError:
                    pass


def load_scenarios_from_cache(cache):
    """Extract cached scenario list from the unified cache dict."""
    scenarios = cache.get("scenarios", [])
    if scenarios:
        logger.debug("Loaded %d scenarios from JSON cache", len(scenarios))
    return scenarios
