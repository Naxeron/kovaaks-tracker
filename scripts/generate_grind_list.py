#!/usr/bin/env python3
"""
Generate Daily Target Grind List for KovaaKs Leaderboard Climbing

Generates an actionable, high-ROI playlist of scenarios to play in your next session.
"""

import gzip
import json
import os
import sys

def generate_grind_list(top_n=10):
    cache_path = 'data/scores_cache.json.gz'
    if not os.path.exists(cache_path):
        print(f"Error: {cache_path} not found.")
        return

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
    print(f"            YOUR HIGH-ROI KOVAAKS TARGET LIST (Top {top_n} Each Category)")
    print("==========================================================================")

    # 1. Quick Fixes (High entries, low percentile)
    underperformed = [s for s in played if s['pct'] < 75 and s['entries'] >= 500]
    underperformed.sort(key=lambda x: (x['entries'] * (0.90 - x['pct']/100.0)), reverse=True)

    print(f"\n⚡ CATEGORY 1: QUICK FIXES (Underperformed Played Scenarios)")
    print("   Goal: Fix your score to ~90th percentile for massive instant point jumps.")
    for i, s in enumerate(underperformed[:top_n], 1):
        target_pts = int(s['entries'] * 0.90) - s['pts']
        print(f"   {i:2d}. {s['name']:40s} | Rank: {s['rank']:6d}/{s['entries']:6d} ({s['pct']:4.1f}%) | Est Gain @ 90%: +{target_pts:6,d} pts")

    # 2. Mega-Population Pushes
    mega = [s for s in played if s['entries'] >= 20000]
    mega.sort(key=lambda x: (x['entries'] * 0.01), reverse=True)

    print(f"\n🔥 CATEGORY 2: MEGA-POPULATION PUSHES (High Entry Count)")
    print("   Goal: Improve rank by just 0.5% - 1% on huge player base scenarios.")
    for i, s in enumerate(mega[:top_n], 1):
        gain_1pct = int(s['entries'] * 0.01)
        print(f"   {i:2d}. {s['name']:40s} | Entries: {s['entries']:7,d} | Rank: {s['rank']:6d} ({s['pct']:4.1f}%) | 1% Push Gain: +{gain_1pct:6,d} pts")

    # 3. High-Value Unplayed Scenarios
    unplayed.sort(key=lambda x: x['entries'], reverse=True)

    print(f"\n🎯 CATEGORY 3: HIGH-VALUE UNPLAYED SCENARIOS")
    print("   Goal: Play these scenarios for the first time for guaranteed high points.")
    for i, s in enumerate(unplayed[:top_n], 1):
        est_pts = int(s['entries'] * (avg_pct / 100.0))
        print(f"   {i:2d}. {s['name']:40s} | Entries: {s['entries']:7,d} | Est Gain @ {avg_pct:.0f}%: +{est_pts:6,d} pts")

    print("\n==========================================================================")

if __name__ == '__main__':
    top_n = 10
    if len(sys.argv) > 1:
        try:
            top_n = int(sys.argv[1])
        except ValueError:
            pass
    generate_grind_list(top_n)
