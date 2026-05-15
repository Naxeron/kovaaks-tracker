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
    if not entry_history:
        print("No entry history found in cache.")
        return

    scenarios = cache.get("scenarios", [])
    scenario_names = {str(s.get("leaderboardId")): s.get("scenarioName") for s in scenarios}

    print(f"Found history for {len(entry_history)} scenarios.")
    
    # Pick a few scenarios with interesting history
    picked = []
    for lid, hist in entry_history.items():
        if len(hist) >= 5:
            diff = max(hist.values()) - min(hist.values())
            if diff > 10:
                picked.append(lid)
        if len(picked) >= 3:
            break

    if not picked:
        # Fallback to any 3
        picked = list(entry_history.keys())[:3]

    for lid in picked:
        hist = entry_history[lid]
        sname = scenario_names.get(lid, f"Unknown (lid={lid})")
        print(f"\n--- Scenario: {sname} ---")
        
        dates = sorted(hist.keys())
        for d in dates:
            print(f"  {d}: {hist[d]}")

        # Replicate GUI logic
        d0_str = dates[0].replace("Z", "+00:00")
        if len(d0_str) <= 10: d0_str += "T00:00:00"
        oldest = datetime.datetime.fromisoformat(d0_str).replace(tzinfo=None)
        
        d1_str = dates[-1].replace("Z", "+00:00")
        if len(d1_str) <= 10: d1_str += "T00:00:00"
        newest = datetime.datetime.fromisoformat(d1_str).replace(tzinfo=None)
        
        seconds_diff = (newest - oldest).total_seconds()
        print(f"\n  Total history span: {seconds_diff/3600:.1f} hours")
        
        if seconds_diff >= 1800:
            # 1. Total trend (days)
            days_diff = seconds_diff / 86400.0
            entry_diff_total = hist[dates[-1]] - hist[dates[0]]
            popularity_trend = float(entry_diff_total) / days_diff
            print(f"  Projected Trend (per day): {popularity_trend:.2f}")
            
            # 2. 24h delta
            target_24h = newest - datetime.timedelta(days=1)
            idx_24h = 0
            found_dt = None
            for i in range(len(dates) - 1, -1, -1):
                ds = dates[i].replace("Z", "+00:00")
                if len(ds) <= 10: ds += "T00:00:00"
                dt = datetime.datetime.fromisoformat(ds).replace(tzinfo=None)
                if dt <= target_24h:
                    idx_24h = i
                    found_dt = dt
                    break
            
            if found_dt:
                actual_new_entries = hist[dates[-1]] - hist[dates[idx_24h]]
                print(f"  Found 24h point: {dates[idx_24h]} ({ (newest - found_dt).total_seconds()/3600:.1f} hours ago)")
                print(f"  New Entries (24h): {actual_new_entries}")
            else:
                print("  Could not find a point >= 24h ago. Using oldest available.")
                actual_new_entries = hist[dates[-1]] - hist[dates[0]]
                print(f"  New Entries (available history): {actual_new_entries}")
            
            mult = max(0.2, math.log10(max(1.0, popularity_trend + 1.0)) / 2.0)
            print(f"  Competition Multiplier: {mult:.3f}x")

if __name__ == "__main__":
    verify()
