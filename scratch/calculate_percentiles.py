import os
import gzip
import json
import statistics

def main():
    project_dir = "/home/naxeron/Projects/kovaaks"
    cache_path = os.path.join(project_dir, "data", "scores_cache.json.gz")
    
    if not os.path.exists(cache_path):
        print(f"Error: Cache file not found at {cache_path}")
        return

    with gzip.open(cache_path, "rt", encoding="utf-8") as f:
        cache = json.load(f)
    
    scenarios = cache.get("scenarios", [])
    scores = cache.get("scores", {})
    
    # Map leaderboardId to scenario info
    scenario_info = {}
    for s in scenarios:
        lid = str(s.get("leaderboardId", ""))
        scenario_info[lid] = {
            "name": s.get("scenarioName", ""),
            "entries": s.get("counts", {}).get("entries", 0),
            "aimType": s.get("scenario", {}).get("aimType", "Unknown"),
        }
    
    aim_type_data = {}
    
    for lid, cached in scores.items():
        if lid in scenario_info:
            info = scenario_info[lid]
            user = cached.get("user")
            if not user:
                continue
            
            try:
                rank = int(user.get("rank"))
                score = float(user.get("score"))
            except (ValueError, TypeError):
                continue
                
            entries = int(info.get("entries", 0))
            if entries <= 0:
                continue
                
            # Calculate percentile: percentage of players the user beat
            # e.g., if rank 1 out of 100, (1 - 1/100)*100 = 99%
            percentile = (1 - rank / entries) * 100
            
            aim_type = info["aimType"] or "Unknown"
            if aim_type not in aim_type_data:
                aim_type_data[aim_type] = []
                
            aim_type_data[aim_type].append({
                "name": info["name"],
                "rank": rank,
                "entries": entries,
                "percentile": percentile,
                "score": score
            })

    print(f"Scenario Type Summary:")
    print("-" * 80)
    for atype, items in sorted(aim_type_data.items()):
        pcts = [item["percentile"] for item in items]
        avg_pct = statistics.mean(pcts)
        med_pct = statistics.median(pcts)
        best = max(items, key=lambda x: x["percentile"])
        worst = min(items, key=lambda x: x["percentile"])
        
        print(f"Aim Type: {atype}")
        print(f"  Played Scenarios: {len(items)}")
        print(f"  Average Percentile: {avg_pct:.2f}%")
        print(f"  Median Percentile: {med_pct:.2f}%")
        print(f"  Best Scenario: {best['name']} ({best['percentile']:.2f}% - Rank {best['rank']}/{best['entries']})")
        print(f"  Worst Scenario: {worst['name']} ({worst['percentile']:.2f}% - Rank {worst['rank']}/{worst['entries']})")
        print("-" * 80)

if __name__ == "__main__":
    main()
