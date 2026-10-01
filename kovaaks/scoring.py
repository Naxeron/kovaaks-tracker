"""
KovaaKs Scenario Tracker — scoring and potential estimation utilities.

Encapsulates popularity trends and the multi-factor priority algorithm.
"""

import datetime
import math
import logging

from .history import CompactHistory

logger = logging.getLogger("kovaaks")


def parse_iso_dt(s):
    """Parse an ISO 8601 datetime string, replacing 'Z' and stripping timezone info."""
    ds = s.replace("Z", "+00:00")
    if len(ds) <= 10:
        ds += "T00:00:00"
    return datetime.datetime.fromisoformat(ds).replace(tzinfo=None)


def prune_entry_history(history, now_datetime=None, limit=168):
    """Bound every scenario's history, including scenarios outside UI filters.

    Shared hourly timestamps and compact retention plans are checked once per
    pass. Discard malformed and implausibly future samples before retaining
    the newest valid samples.
    """
    now = now_datetime or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cutoff = now + datetime.timedelta(hours=1)
    parsed = {}
    retained_axes = {}
    changed = False
    for points in history.values():
        if isinstance(points, CompactHistory) and points.is_compact:
            stamps = points.timestamps
            # Identity avoids hashing the same long tuple for every scenario.
            # Keep the tuple alive in this pass so its identity cannot be reused.
            plan = retained_axes.get(id(stamps))
            if plan is None:
                valid = []
                for stamp in stamps:
                    if stamp not in parsed:
                        try:
                            parsed[stamp] = parse_iso_dt(stamp)
                        except (ValueError, TypeError, AttributeError):
                            parsed[stamp] = None
                    if parsed[stamp] is not None and parsed[stamp] <= cutoff:
                        valid.append(stamp)
                exceeds_limit = len(valid) > limit
                if exceeds_limit:
                    oldest = set(sorted(valid, key=parsed.__getitem__)[:-limit])
                    valid = [stamp for stamp in valid if stamp not in oldest]
                retained = (stamps if len(valid) == len(stamps)
                            else CompactHistory.prepare_timestamps(valid))
                plan = retained_axes[id(stamps)] = (stamps, retained, exceeds_limit)
            changed = plan[2] or changed
            if plan[1] is not stamps:
                points.retain_timestamps(plan[1])
                changed = True
            continue
        for stamp in list(points):
            if stamp not in parsed:
                try:
                    parsed[stamp] = parse_iso_dt(stamp)
                except (ValueError, TypeError, AttributeError):
                    parsed[stamp] = None
            if parsed[stamp] is None or parsed[stamp] > cutoff:
                del points[stamp]
                changed = True
        if len(points) > limit:
            oldest = sorted(points, key=parsed.__getitem__)[:-limit]
            for stamp in oldest:
                del points[stamp]
            changed = True
    return changed


def parse_popularity_metrics(hist, now_datetime=None, *, timeline_cache=None):
    """Calculate popularity trend and actual new entries in the last 24 hours.

    A caller-owned ``timeline_cache`` shares timestamp calculations across one
    batch of compact histories; scenario-specific counts are always read anew.

    Returns:
        tuple: (popularity_trend, actual_new_entries)
    """
    popularity_trend = 0.0
    actual_new_entries = 0
    if not hist or len(hist) < 2:
        return popularity_trend, actual_new_entries

    try:
        stamps = (hist.timestamps if timeline_cache is not None
                  and isinstance(hist, CompactHistory) and hist.is_compact else None)
        plan = timeline_cache.get(id(stamps)) if stamps is not None else None
        if plan is None:
            dates = sorted(hist.keys())
            first_stamp, last_stamp = dates[0], dates[-1]
            oldest = parse_iso_dt(first_stamp)
            newest = parse_iso_dt(last_stamp)
            seconds_diff = (newest - oldest).total_seconds()
            day_stamp = None
        else:
            _, first_stamp, last_stamp, seconds_diff, day_stamp = plan
        
        if seconds_diff >= 1800:  # Need at least 30 minutes
            popularity_trend = (hist[last_stamp] - hist[first_stamp]) / (seconds_diff / 86400.0)
            
            if plan is None:
                target_24h = newest - datetime.timedelta(days=1)
                idx_24h = 0
                for i in range(len(dates) - 1, -1, -1):
                    if parse_iso_dt(dates[i]) <= target_24h:
                        idx_24h = i
                        break
                day_stamp = dates[idx_24h]
            actual_new_entries = hist[last_stamp] - hist[day_stamp]
        if stamps is not None and plan is None:
            # Retain the source tuple so its identity cannot be reused while
            # this batch is active, including if a history changes its axis.
            timeline_cache[id(stamps)] = (stamps, first_stamp, last_stamp, seconds_diff, day_stamp)
    except (ValueError, TypeError, OSError) as e:
        logger.debug("Failed to parse popularity metrics: %s", e)

    return popularity_trend, actual_new_entries


def calculate_potential_score(rank, entries, lstats, now, competition_multiplier, expected_pct=None):
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
        if expected_pct is None:
            skill_gap = 1.0 - pct / 100.0
        else:
            target_pct = max(expected_pct, pct + (100.0 - pct) * 0.1)
            skill_gap = (target_pct - pct) / 100.0
            
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
