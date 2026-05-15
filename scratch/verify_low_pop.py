import gzip
import json
import datetime
import math

CACHE_PATH = "scores_cache.json.gz"

def verify():
    try:
        with gzip.open(CACHE_PATH, "rt", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception as e:
        print(f"Error loading cache: {e}")
        return

    entry_history = cache.get("entry_history", {})
    scenarios = cache.get("scenarios", [])
    scenario_names = {str(s.get("leaderboardId")): s.get("scenarioName") for s in scenarios}

    # Find scenarios with very few changes
    picked = []
    for lid, hist in entry_history.items():
        if len(hist) >= 5:
            diff = max(hist.values()) - min(hist.values())
            if 0 < diff <= 3:
                picked.append(lid)
        if len(picked) >= 5:
            break

    for lid in picked:
        hist = entry_history[lid]
        sname = scenario_names.get(lid, f"Unknown (lid={lid})")
        print(f"\n--- Scenario: {sname} ---")
        
        dates = sorted(hist.keys())
        d0_str = dates[0].replace("Z", "+00:00")
        if len(d0_str) <= 10: d0_str += "T00:00:00"
        oldest = datetime.datetime.fromisoformat(d0_str).replace(tzinfo=None)
        
        d1_str = dates[-1].replace("Z", "+00:00")
        if len(d1_str) <= 10: d1_str += "T00:00:00"
        newest = datetime.datetime.fromisoformat(d1_str).replace(tzinfo=None)
        
        seconds_diff = (newest - oldest).total_seconds()
        days_diff = seconds_diff / 86400.0
        entry_diff_total = hist[dates[-1]] - hist[dates[0]]
        popularity_trend = float(entry_diff_total) / days_diff
        
        print(f"  Entry Diff: {entry_diff_total} over {seconds_diff/3600:.1f} hours")
        print(f"  Old Display (Projected / Day): {popularity_trend:.1f}")
        print(f"  New Display (24h Actual): {entry_diff_total} (assuming <24h history for now)")

if __name__ == "__main__":
    verify()
