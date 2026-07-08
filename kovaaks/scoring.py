"""
KovaaKs Scenario Tracker — scoring and potential estimation utilities.

Encapsulates popularity trends and the multi-factor priority algorithm.
"""

import datetime
import math
import logging

logger = logging.getLogger("kovaaks")


def parse_iso_dt(s):
    """Parse an ISO 8601 datetime string, replacing 'Z' and stripping timezone info."""
    ds = s.replace("Z", "+00:00")
    if len(ds) <= 10:
        ds += "T00:00:00"
    return datetime.datetime.fromisoformat(ds).replace(tzinfo=None)


def parse_popularity_metrics(hist, now_datetime=None):
    """Calculate popularity trend and actual new entries in the last 24 hours.

    Returns:
        tuple: (popularity_trend, actual_new_entries)
    """
    popularity_trend = 0.0
    actual_new_entries = 0
    if not hist or len(hist) < 2:
        return popularity_trend, actual_new_entries

    try:
        dates = sorted(hist.keys())
        oldest = parse_iso_dt(dates[0])
        newest = parse_iso_dt(dates[-1])
        seconds_diff = (newest - oldest).total_seconds()
        
        if seconds_diff >= 1800:  # Need at least 30 minutes
            popularity_trend = (hist[dates[-1]] - hist[dates[0]]) / (seconds_diff / 86400.0)
            
            target_24h = newest - datetime.timedelta(days=1)
            idx_24h = 0
            for i in range(len(dates) - 1, -1, -1):
                if parse_iso_dt(dates[i]) <= target_24h:
                    idx_24h = i
                    break
            actual_new_entries = hist[dates[-1]] - hist[dates[idx_24h]]
    except (ValueError, TypeError, OSError) as e:
        logger.debug("Failed to parse popularity metrics: %s", e)

    return popularity_trend, actual_new_entries


def calculate_potential_score(rank, entries, lstats, now, competition_multiplier):
    """Calculate Potential Score using a multi-factor priority algorithm.

    1. Logarithmic Potential — neutralizes population bias
    2. Spaced Repetition (Time Factor) — Ebbinghaus curve
    3. Session Fatigue — decoupled from PB tracking
    4. Variance-Modulated Plateau Penalty (Sigmoid Decay)
    5. Active Learning Bonus — clamped trend factor
    6. Competition Multiplier
    """
    if entries <= 0 or rank <= 0 or rank > entries:
        return 0

    try:
        pct = (1 - rank / entries) * 100
        
        # 1. Logarithmic Potential
        skill_gap = 1.0 - pct / 100.0
        log_weight = math.log10(max(rank, 10))
        base_potential = log_weight * skill_gap

        # 2. Spaced Repetition (Time Factor)
        if lstats.get("last_played"):
            last_played = lstats["last_played"]
            if isinstance(last_played, str):
                try:
                    last_played = datetime.datetime.fromisoformat(last_played)
                except ValueError:
                    last_played = now
            days_ago = (now - last_played).total_seconds() / 86400.0
            time_factor = 0.8 + 0.7 * (1.0 - math.exp(-max(0.0, days_ago) / 14.0))
        else:
            time_factor = 1.5  # Maximum priority for unplayed benchmarks

        # 3. Session Fatigue
        runs_today = lstats.get("runs_today", 0)
        fatigue_factor = math.exp(-runs_today / 12.0)

        # 4. Variance-Modulated Plateau Penalty (Sigmoid Decay)
        pb_ago = lstats.get("runs_since_recent_pb", 0)
        trend = lstats.get("trend", 1.0)
        if trend <= 1.02:
            plateau_penalty = 1.0 - (0.85 / (1.0 + math.exp(-0.4 * (pb_ago - 20.0))))
        else:
            plateau_penalty = 1.0

        # 5. Active Learning Bonus
        trend_factor = max(0.8, min(trend, 1.3))

        # 6. Final Potential
        potential = (base_potential * 1000) * time_factor * fatigue_factor * plateau_penalty * trend_factor * competition_multiplier
        return int(potential)
    except (ValueError, TypeError, ZeroDivisionError) as e:
        logger.warning("Error calculating potential score: %s", e)
        return 0
