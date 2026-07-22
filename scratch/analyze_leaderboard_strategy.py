import gzip
import json
import os
import sys

def analyze():
    cache_path = 'data/scores_cache.json.gz'
    if not os.path.exists(cache_path):
        print(f"Error: {cache_path} not found.")
        return

    with gzip.open(cache_path, 'rt') as f:
        cache = json.load(f)

    scenarios = cache.get('scenarios', [])
    scores = cache.get('scores', {})

    total_scenarios = len(scenarios)
    played_scenarios = []
    unplayed_scenarios = []

    user_points = 0
    potential_unplayed_pts_avg_pct = 0
    potential_unplayed_pts_95_pct = 0
    potential_unplayed_pts_99_pct = 0

    # Collect stats for played scenarios
    percentiles = []
    ranks = []

    for item in scenarios:
        lid = str(item.get('leaderboardId'))
        entries = item.get('counts', {}).get('entries', 0)
        scen_name = item.get('scenarioName', '')

        if not lid or entries <= 0:
            continue

        score_info = scores.get(lid, {})
        user_info = score_info.get('user')

        if user_info and user_info.get('rank'):
            rank = user_info['rank']
            if rank <= entries:
                pts = entries - rank
                pct = (1.0 - rank / entries) * 100.0
                user_points += pts
                percentiles.append(pct)
                ranks.append((rank, entries))
                played_scenarios.append({
                    'lid': lid,
                    'name': scen_name,
                    'entries': entries,
                    'rank': rank,
                    'points': pts,
                    'pct': pct,
                    'max_possible_points': entries - 1,
                    'pts_lost': rank - 1
                })
        else:
            unplayed_scenarios.append({
                'lid': lid,
                'name': scen_name,
                'entries': entries,
                'max_possible_points': entries - 1
            })

    num_played = len(played_scenarios)
    num_unplayed = len(unplayed_scenarios)

    avg_pct = sum(percentiles) / len(percentiles) if percentiles else 0
    median_pct = sorted(percentiles)[len(percentiles) // 2] if percentiles else 0

    print("==================================================")
    print("      KOVAAKS LEADERBOARD STRATEGY ANALYSIS      ")
    print("==================================================")
    print(f"Total Scenarios in Cache: {total_scenarios}")
    print(f"Played Scenarios:        {num_played} ({num_played/total_scenarios*100:.1f}%)")
    print(f"Unplayed Scenarios:      {num_unplayed} ({num_unplayed/total_scenarios*100:.1f}%)")
    print(f"Calculated Total Points: {user_points:,}")
    print(f"Average Percentile:      {avg_pct:.2f}%")
    print(f"Median Percentile:       {median_pct:.2f}%")
    print("--------------------------------------------------")

    # Analyze unplayed scenarios by size brackets
    brackets = [
        ("> 10,000 entries", lambda e: e > 10000),
        ("1,000 - 10,000 entries", lambda e: 1000 <= e <= 10000),
        ("100 - 1,000 entries", lambda e: 100 <= e < 1000),
        ("< 100 entries", lambda e: e < 100),
    ]

    print("\n--- UNPLAYED SCENARIOS POTENTIAL ---")
    total_unplayed_potential = 0
    for b_name, b_filter in brackets:
        matching = [s for s in unplayed_scenarios if b_filter(s['entries'])]
        total_entries = sum(s['entries'] for s in matching)
        # Potential at avg percentile
        est_pts_avg = sum(int(s['entries'] * (avg_pct / 100.0)) for s in matching)
        est_pts_95 = sum(int(s['entries'] * 0.95) for s in matching)
        est_pts_99 = sum(int(s['entries'] * 0.99) for s in matching)
        total_unplayed_potential += est_pts_avg
        print(f"Bracket {b_name:25s}: {len(matching):5d} scenarios | Est. Pts @ {avg_pct:.1f}%: {est_pts_avg:10,d} | Est @ 99%: {est_pts_99:10,d}")

    print(f"TOTAL EST. GAIN from unplayed (at average skill level {avg_pct:.1f}%): +{total_unplayed_potential:,} points")

    # Analyze played scenarios improvement potential
    print("\n--- PLAYED SCENARIOS IMPROVEMENT POTENTIAL ---")
    played_scenarios.sort(key=lambda x: x['pts_lost'], reverse=True)
    total_pts_lost = sum(s['pts_lost'] for s in played_scenarios)
    print(f"Total Points Lost (difference between Rank 1 and current rank): {total_pts_lost:,} points")

    # How many points can be gained if improving to 99th percentile or top 10 rank on played scenarios?
    potential_improvement_to_99 = 0
    potential_improvement_to_top10 = 0
    for s in played_scenarios:
        current_rank = s['rank']
        entries = s['entries']

        # target 99th percentile rank = max(1, int(entries * 0.01))
        target_rank_99 = max(1, int(entries * 0.01))
        if current_rank > target_rank_99:
            potential_improvement_to_99 += (current_rank - target_rank_99)

        target_rank_top10 = min(current_rank, 10)
        potential_improvement_to_top10 += (current_rank - target_rank_top10)

    print(f"Est. Gain if improving all played scenarios <99% up to 99th percentile: +{potential_improvement_to_99:,} points")
    print(f"Est. Gain if improving all played scenarios to Top 10 rank:         +{potential_improvement_to_top10:,} points")

    print("\n--- TOP 10 PLAYED SCENARIOS WITH LARGEST UNTAPPED POINTS (Highest Points Lost) ---")
    for i, s in enumerate(played_scenarios[:10], 1):
        print(f"{i:2d}. {s['name'][:40]:40s} | Rank: {s['rank']:6d}/{s['entries']:6d} ({s['pct']:.2f}%) | Current Pts: {s['points']:7,d} | Pts Lost: +{s['pts_lost']:7,d}")

    print("\n--- TOP 10 UNPLAYED SCENARIOS BY ENTRY COUNT (Easiest Massive Points) ---")
    unplayed_scenarios.sort(key=lambda x: x['entries'], reverse=True)
    for i, s in enumerate(unplayed_scenarios[:10], 1):
        est_pts = int(s['entries'] * (avg_pct / 100.0))
        print(f"{i:2d}. {s['name'][:40]:40s} | Entries: {s['entries']:7,d} | Est. Pts @ {avg_pct:.1f}%: +{est_pts:7,d}")

if __name__ == '__main__':
    analyze()
