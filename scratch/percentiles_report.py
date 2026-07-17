import os
import gzip
import json
import statistics

def classify(name, raw_type):
    if not raw_type:
        raw_type = ""
    raw_lower = raw_type.lower()
    name_lower = str(name).lower()
    
    if "tracking" in raw_lower or "strafe" in raw_lower:
        return "Tracking"
    if "clicking" in raw_lower or "flick" in raw_lower or "static" in raw_lower:
        return "Clicking / Static"
    if "switching" in raw_lower or "ts" in raw_lower:
        return "Target Switching"
        
    # Name checks
    if "tracking" in name_lower or "strafe" in name_lower or "lg " in name_lower or "smooth" in name_lower or "centered" in name_lower or "centering" in name_lower or "shaft" in name_lower or "reactive" in name_lower:
        return "Tracking"
    if "click" in name_lower or "static" in name_lower or "flick" in name_lower or "popcorn" in name_lower or "pokeball" in name_lower or "1wall" in name_lower or "tile frenzy" in name_lower or "microshot" in name_lower or "pasu" in name_lower or "reflex" in name_lower:
        return "Clicking / Static"
    if "switching" in name_lower or " ts" in name_lower or "target switch" in name_lower or "ts " in name_lower:
        return "Target Switching"
        
    if "other" in raw_lower:
        return "Other"
    return "Other / Unclassified"

def main():
    project_dir = "/home/naxeron/Projects/kovaaks"
    cache_path = os.path.join(project_dir, "data", "scores_cache.json.gz")
    
    with gzip.open(cache_path, "rt", encoding="utf-8") as f:
        d = json.load(f)

    scenarios = {str(s.get("leaderboardId", "")): {
        "name": s.get("scenarioName", ""),
        "entries": s.get("counts", {}).get("entries", 0),
        "aimType": s.get("scenario", {}).get("aimType", "Unknown"),
    } for s in d.get("scenarios", [])}

    all_items = []
    grouped = {}
    
    for lid, cached in d.get("scores", {}).items():
        if lid in scenarios:
            info = scenarios[lid]
            user = cached.get("user")
            if not user or not user.get("rank"):
                continue
            try:
                rank = int(user["rank"])
                entries = int(info["entries"])
                if entries <= 0:
                    continue
                pct = (1 - rank / entries) * 100
                score = float(user["score"])
                date = user.get("date", "Unknown")
                
                cleaned = classify(info["name"], info["aimType"])
                
                item = {
                    "name": info["name"],
                    "rank": rank,
                    "entries": entries,
                    "percentile": pct,
                    "score": score,
                    "date": date,
                    "category": cleaned
                }
                
                all_items.append(item)
                grouped.setdefault(cleaned, []).append(item)
            except ValueError:
                continue

    if not all_items:
        print("No scored scenarios found.")
        return

    all_pcts = [x["percentile"] for x in all_items]
    print(f"# KovaaKs Percentile Analysis Report")
    print(f"**Total Scenarios Scored:** {len(all_items)}")
    print(f"**Overall Average Percentile:** {statistics.mean(all_pcts):.2f}%")
    print(f"**Overall Median Percentile:** {statistics.median(all_pcts):.2f}%")
    print("\n## Breakdown by Scenario Type\n")
    
    print("| Scenario Type | Played | Average Percentile | Median Percentile |")
    print("|---|---|---|---|")
    for cat in sorted(grouped.keys()):
        pcts = [x["percentile"] for x in grouped[cat]]
        print(f"| {cat} | {len(grouped[cat])} | {statistics.mean(pcts):.2f}% | {statistics.median(pcts):.2f}% |")

    for cat in ["Tracking", "Clicking / Static", "Target Switching"]:
        if cat not in grouped:
            continue
        items = sorted(grouped[cat], key=lambda x: -x["percentile"])
        print(f"\n### Top 5 in {cat}\n")
        print("| Scenario Name | Percentile | Rank | Score | Date |")
        print("|---|---|---|---|---|")
        for x in items[:5]:
            print(f"| {x['name']} | {x['percentile']:.2f}% | {x['rank']}/{x['entries']} | {x['score']:.1f} | {x['date']} |")

        print(f"\n### Bottom 3 in {cat}\n")
        print("| Scenario Name | Percentile | Rank | Score | Date |")
        print("|---|---|---|---|---|")
        for x in reversed(items[-3:]):
            print(f"| {x['name']} | {x['percentile']:.2f}% | {x['rank']}/{x['entries']} | {x['score']:.1f} | {x['date']} |")

if __name__ == "__main__":
    main()
