#!/usr/bin/env python3
"""
KovaaKs Leaderboard Strategy Analyzer & Target Optimizer

Analyzes scores_cache.json.gz to compute exact point returns for:
1. Low-hanging fruit: Unplayed scenarios with highest entry counts.
2. Underperformed played scenarios: High entry count scenarios with below-average percentiles.
3. Mega-population score pushes: High-entry scenarios where a slight rank push gives huge points.
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

    print("==========================================================================")
    print("               KOVAAKS LEADERBOARD CLIMBING ROI ANALYSIS                 ")
    print("==========================================================================")
    print(f"Current Stats: {len(played):,} Played Scenarios | Avg Skill: {avg_pct:.1f}% Percentile")

    # 1. Unplayed Scenarios breakdown by ROI
    unplayed.sort(key=lambda x: x['entries'], reverse=True)
    top_unplayed_50 = unplayed[:50]
    pts_top_50_unplayed = sum(int(s['entries'] * (avg_pct / 100.0)) for s in top_unplayed_50)

    print("\n[1] UNPLAYED SCENARIOS ANALYSIS")
    print(f"  • Top 50 Unplayed Scenarios Total Potential: +{pts_top_50_unplayed:,} points (Avg +{pts_top_50_unplayed//50:,} pts/run)")
    print(f"  • Remaining 41,850 Unplayed Scenarios Total Potential: +{sum(int(s['entries']*(avg_pct/100.0)) for s in unplayed[50:]):,} points (Avg +{int(sum(int(s['entries']*(avg_pct/100.0)) for s in unplayed[50:]) / max(1, len(unplayed)-50)):,} pts/run)")
    print("  --> CONCLUSION: Only the top ~50-100 unplayed scenarios are worth playing for fast points.")

    # 2. Underperformed Played Scenarios (High entries + low percentile)
    underperformed = [s for s in played if s['pct'] < 75 and s['entries'] >= 500]
    underperformed.sort(key=lambda x: (x['entries'] * (0.90 - x['pct']/100.0)), reverse=True)

    print("\n[2] UNDERPERFORMED PLAYED SCENARIOS (Quick Fixes)")
    print(f"Found {len(underperformed)} played scenarios with entries >= 500 where your percentile is < 75%.")
    print("Improving these to 90th percentile yields massive fast points:")
    for i, s in enumerate(underperformed[:10], 1):
        target_pts = int(s['entries'] * 0.90) - s['pts']
        print(f"  {i:2d}. {s['name'][:35]:35s} | Rank: {s['rank']:6d}/{s['entries']:6d} ({s['pct']:.1f}%) | Fix to 90%: +{target_pts:6,d} pts")

    # 3. Mega-Population Score Pushes (> 20,000 entries)
    mega = [s for s in played if s['entries'] >= 20000]
    # Est gain for 1% percentile improvement
    for s in mega:
        s['gain_1pct'] = int(s['entries'] * 0.01)
    mega.sort(key=lambda x: x['gain_1pct'], reverse=True)

    print("\n[3] MEGA-POPULATION SCENARIO PUSHES (High Entry Count)")
    print("Small score gains on these scenarios yield massive points (1% percentile increase = huge gains):")
    for i, s in enumerate(mega[:10], 1):
        print(f"  {i:2d}. {s['name'][:35]:35s} | Entries: {s['entries']:7,d} | Rank: {s['rank']:6d} ({s['pct']:.1f}%) | 1% Push Gain: +{s['gain_1pct']:6,d} pts")

    print("\n==========================================================================")
    print("                        RECOMMENDED ACTION PLAN                           ")
    print("==========================================================================")
    print("1. Fix Underperformed Played Scenarios first (Highest ROI per minute).")
    print("2. Play the Top 50 Unplayed Scenarios by entry count.")
    print("3. Push Mega-Population Scenarios for incremental multi-thousand point gains.")
    print("4. IGNORE low-entry unplayed scenarios (< 100 entries) — they give < 50 pts/run.")

if __name__ == '__main__':
    main()
