"""
KovaaKs Scenario Tracker — cache utilities.

Handles loading and saving the gzip-compressed JSON scores cache.
"""

import gzip
import json
import logging
import os

logger = logging.getLogger("kovaaks")

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
SCORES_CACHE = os.path.join(SCRIPT_DIR, "scores_cache.json.gz")


def load_scores_cache():
    """Load the unified gzip JSON cache from disk. Returns empty dict on failure."""
    if not os.path.exists(SCORES_CACHE):
        return {}
    try:
        with gzip.open(SCORES_CACHE, "rt", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Loaded cache from %s", SCORES_CACHE)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not load cache: %s", e)
        return {}


def save_scores_cache(cache_dict):
    """Save the unified cache dict to gzip-compressed JSON."""
    try:
        with gzip.open(SCORES_CACHE, "wt", encoding="utf-8") as f:
            json.dump(cache_dict, f, separators=(",", ":"))
    except OSError as e:
        logger.warning("Could not save cache: %s", e)


def load_scenarios_from_cache(cache):
    """Extract cached scenario list from the unified cache dict."""
    scenarios = cache.get("scenarios", [])
    if scenarios:
        logger.debug("Loaded %d scenarios from JSON cache", len(scenarios))
    return scenarios
