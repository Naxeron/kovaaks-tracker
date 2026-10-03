"""
KovaaKs Scenario Tracker — scoring and potential estimation utilities.

Encapsulates popularity trends and the multi-factor priority algorithm.
"""

import datetime
import math
import logging
from collections.abc import Mapping, Sequence

from .data_processing import safe_float, safe_int
from .history import CompactHistory

logger = logging.getLogger("kovaaks")


def calculate_rank_percentile(rank, entries):
    """Return a valid leaderboard percentile, or None for inconsistent input."""
    try:
        position, population = safe_float(rank, None), safe_float(entries, None)
        if (isinstance(rank, bool) or isinstance(entries, bool)
                or position is None or population is None
                or not math.isfinite(position) or not math.isfinite(population)
                or not position.is_integer() or not population.is_integer()
                or not 1 <= position <= population):
            return None
        return (1.0 - position / population) * 100.0
    except OverflowError:
        return None


def calculate_global_points(scenarios, scores):
    """Sum unique cached leaderboard contributions before any display filters.

    Official global points are entries minus the user's rank on each scenario.
    Use the full catalog's accurate entry counts and cached API ranks, including
    hidden and low-population scenarios. Missing scores and inconsistent ranks
    cannot establish a contribution and are ignored.
    """
    if (not isinstance(scenarios, Sequence) or isinstance(scenarios, (str, bytes))
            or not isinstance(scores, Mapping)):
        return 0

    def count(value):
        try:
            number = safe_float(value, None)
        except OverflowError:
            return None
        if (isinstance(value, bool) or number is None or not math.isfinite(number)
                or not number.is_integer()):
            return None
        return safe_int(number, None)

    total = 0
    seen = set()
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            continue
        raw_lid = scenario.get("leaderboardId")
        if not isinstance(raw_lid, (str, int)) or isinstance(raw_lid, bool):
            continue
        lid = str(raw_lid)
        if not lid.strip() or lid in seen:
            continue
        counts = scenario.get("counts")
        cached = scores.get(lid)
        user = cached.get("user") if isinstance(cached, Mapping) else None
        if not isinstance(counts, Mapping) or not isinstance(user, Mapping):
            continue
        entries, rank = count(counts.get("entries")), count(user.get("rank"))
        if entries is not None and rank is not None and 1 <= rank <= entries:
            total += entries - rank
            seen.add(lid)
    return total


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


def calculate_potential_score(rank, entries, lstats=None, now=None,
                              competition_multiplier=1.0, expected_pct=None):
    """Estimate point opportunity, with small evidence-based practice modifiers.

    ``rank=None`` means no submitted score; invalid ranks do not mean unplayed.
    A category percentile supplies a heuristic target, with a 10% remaining-rank
    stretch for players already above that target. This is neither a calibrated
    prediction nor points per minute: durations and score distributions are not
    yet available. ``now`` and ``competition_multiplier`` remain accepted for
    older callers but no longer affect priority.
    """
    try:
        def finite(value, default):
            try:
                number = safe_float(value, default)
            except OverflowError:
                return default
            return number if number is not None and math.isfinite(number) else default

        unplayed = rank is None
        if calculate_rank_percentile(entries if unplayed else rank, entries) is None:
            return 0
        population = safe_float(entries)
        position = population if unplayed else safe_float(rank)
        headroom = position - 1
        if headroom == 0:
            return 0
        stats = lstats if isinstance(lstats, Mapping) else {}

        # 1. Point potential: each gained rank contributes one global point.
        target_pct = max(0.0, min(100.0, finite(expected_pct, 50.0)))
        target_rank = max(1, math.ceil(population * ((100.0 - target_pct) / 100.0) - 1e-9))
        base_potential = max(0.0, position - target_rank)
        if not unplayed:
            base_potential = max(base_potential, headroom * 0.1)

        # 2. Spaced repetition: age alone does not establish attainable gains.
        # 3. Session fatigue: a rolling daily count cannot establish readiness.
        # Both are neutral until actual session performance can support them.

        # 4. Plateau penalty: real observed attempts, never the old 999 sentinel.
        observations = max(0.0, finite(stats.get("pb_observations"), 0.0))
        pb_ago = max(0.0, min(observations - 1, finite(stats.get("runs_since_pb"), 0.0)))
        plateau_penalty = 1.0
        if not unplayed and pb_ago > 20:
            plateau_penalty -= 0.25 * (1.0 - math.exp(-(pb_ago - 20) / 20.0))

        # 5. Active learning: sparse histories stay neutral; evidence is bounded.
        samples = max(0.0, finite(stats.get("recent_sample_count"), 0.0))
        confidence = max(0.0, min(1.0, (samples - 4.0) / 6.0))
        trend = max(0.9, min(1.1, finite(stats.get("trend"), 1.0)))
        trend_factor = 1.0 if unplayed else 1.0 + confidence * (trend - 1.0)

        # 6. Final potential: population growth is informational, not a multiplier.
        potential = base_potential * plateau_penalty * trend_factor
        # Keep fractional stretch opportunities visible at ranks 2-10.
        return min(int(headroom), max(1, round(potential))) if potential > 0 else 0
    except (ValueError, TypeError, OverflowError, ZeroDivisionError) as e:
        logger.warning("Error calculating potential score: %s", e)
        return 0
