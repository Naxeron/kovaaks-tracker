#!/usr/bin/env python3
"""
KovaaKs Leaderboard Strategy Analyzer

Analyzes local cache data to evaluate points allocation and strategy ROI between:
- Playing unplayed scenarios vs.
- Improving scores on already played scenarios.
"""

import gzip
import json
import os
import sys

def main():
    cache_path = 'data/scores_cache.json.gz'
    if not os.path.exists(cache_path):
        print(f"Error: {cache_path} not found.")
        sys.exit(1)

    with gzip.open(cache_path, 'rt') as f:
        cache = json.load(f)

    scenarios = cache.get('scenarios', [])
    scores = cache.get('scores', {})

    played = []
    unplayed = []
    percentiles = []

    for item in scenarios:
        lid = str(item.get('leaderboardId'))
        entries = item.get('counts', {}).get('entries', 0)
        name = item.get('scenarioName', '')

        if not lid or entries <= 0:
            continue

        score_info = scores.get(lid, {})
        user_info = score_info.get('user')

        if user_info and user_info.get('rank'):
            rank = user_info['rank']
            if rank <= entries:
                pct = (1.0 - rank / entries) * 100.0
                pts = entries - rank
                pts_lost = rank - 1
                percentiles.append(pct)
                played.append({
                    'lid': lid,
                    'name': name,
                    'entries': entries,
                    'rank': rank,
                    'pts': pts,
                    'pct': pct,
                    'pts_lost': pts_lost
                })
        else:
            unplayed.append({
                'lid': lid,
                'name': name,
                'entries': entries
            })

    avg_pct = sum(percentiles) / len(percentiles) if percentiles else 80.0
    total_played_pts = sum(s['pts'] for s in played)

    print("==========================================================================")
    print("               KOVAAKS LEADERBOARD CLIMBING STRATEGY ANALYSIS             ")
    print("==========================================================================")
    print(f"Total Scenarios in Cache: {len(scenarios):,}")
    print(f"Played Scenarios:        {len(played):,} ({len(played)/len(scenarios)*100:.1f}%)")
    print(f"Unplayed Scenarios:      {len(unplayed):,} ({len(unplayed)/len(scenarios)*100:.1f}%)")
    print(f"Current Calculated Pts:  {total_played_pts:,}")
    print(f"Average Percentile:      {avg_pct:.1f}%")
    print("--------------------------------------------------------------------------")

    # 1. Unplayed Breakdown
    unplayed.sort(key=lambda x: x['entries'], reverse=True)
    top_50_unplayed = unplayed[:50]
    top_50_pts = sum(int(s['entries'] * (avg_pct / 100.0)) for s in top_50_unplayed)
    rem_unplayed = unplayed[50:]
    rem_pts = sum(int(s['entries'] * (avg_pct / 100.0)) for s in rem_unplayed)

    print("\n[1] UNPLAYED SCENARIOS ROI")
    print(f"  • Top 50 Unplayed Scenarios:      +{top_50_pts:,} pts total  (~{top_50_pts//50:,} pts/run)")
    print(f"  • Remaining {len(rem_unplayed):,} Unplayed: +{rem_pts:,} pts total  (~{rem_pts//max(1, len(rem_unplayed)):,} pts/run)")

    # 2. Played Scenarios Underperformed
    underperformed = [s for s in played if s['pct'] < 75 and s['entries'] >= 500]
    underperformed.sort(key=lambda x: (x['entries'] * (0.90 - x['pct']/100.0)), reverse=True)
    fix_90_pts = sum(int(s['entries'] * 0.90) - s['pts'] for s in underperformed)

    print("\n[2] UNDERPERFORMED PLAYED SCENARIOS ROI (<75% percentile, >=500 entries)")
    print(f"  • Found {len(underperformed)} scenarios matching criteria.")
    print(f"  • Fixing all to 90th percentile yields: +{fix_90_pts:,} points!")

    # 3. Mega Population Pushes
    mega = [s for s in played if s['entries'] >= 20000]
    gain_1pct_total = sum(int(s['entries'] * 0.01) for s in mega)
    print("\n[3] MEGA-POPULATION PLAYED SCENARIOS (>=20,000 entries)")
    print(f"  • Found {len(mega)} mega scenarios.")
    print(f"  • Pushing your score by just 1% on these yields: +{gain_1pct_total:,} points!")

    print("\n==========================================================================")

if __name__ == '__main__':
    main()
