"""
KovaaKs Scenario Tracker — local stats parsing.

Reads KovaaK's stats CSV files from the Steam directory to extract
play counts, recency, trends, and score history.
"""

import datetime
import logging
import os

logger = logging.getLogger("kovaaks")


def _compute_trend_and_pb(scores):
    trend, runs_since_pb = 1.0, 0
    if len(scores) >= 2:
        max_score = max(s[1] for s in scores)
        runs_since_pb = len(scores) - 1 - max(i for i, s in enumerate(scores) if s[1] == max_score)
        if runs_since_pb == len(scores) - 1:
            runs_since_pb = 999
        avg_change = (scores[-1][1] - scores[0][1]) / (len(scores) - 1)
        if max_score > 1.0:
            trend = max(0.5, min(2.0, 1.0 + (avg_change / max_score) * 5.0))
    return trend, runs_since_pb

def get_local_stats(stats_dir, cache_dict=None):
    """Extract local scenario stats (counts, recency, trends) from the Steam stats directory.
    
    If cache_dict is provided, it performs incremental parsing using the cached
    stats to avoid opening historical CSV files.
    """
    if not os.path.exists(stats_dir):
        logger.warning("Stats directory not found: %s", stats_dir)
        return {}

    try:
        current_files = set(f for f in os.listdir(stats_dir) if f.endswith(" Stats.csv"))
    except OSError as e:
        logger.error("Error listing stats directory: %s", e)
        return {}

    now_dt = datetime.datetime.now()

    # Determine if we can do incremental loading
    use_incremental = False
    if cache_dict is not None:
        if "local_stats" not in cache_dict:
            cache_dict["local_stats"] = {}
        if "known_stat_files" not in cache_dict:
            cache_dict["known_stat_files"] = []
        use_incremental = True

    if use_incremental:
        known_files = set(cache_dict["known_stat_files"])
        new_files = current_files - known_files
        local_stats_cache = cache_dict["local_stats"]

        # Parse new files and update cached stats
        if new_files:
            cache_dict["_dirty"] = True
            newly_played = cache_dict.setdefault("newly_played_scenarios", [])
            # Sort chronologically (oldest to newest) to process runs in order
            sorted_new_files = []
            for fname in new_files:
                parts = fname[:-10].rsplit(" - ", 2)
                if len(parts) < 3:
                    continue
                sname, _, date_str = parts
                try:
                    dt = datetime.datetime.strptime(date_str, "%Y.%m.%d-%H.%M.%S")
                    sorted_new_files.append((dt, fname, sname))
                    if sname not in newly_played:
                        newly_played.append(sname)
                except ValueError:
                    continue

            sorted_new_files.sort(key=lambda x: x[0])

            for dt, fname, sname in sorted_new_files:
                # Initialize scenario in cache if not exists
                if sname not in local_stats_cache:
                    local_stats_cache[sname] = {
                        "count": 0,
                        "last_played": dt.isoformat(),
                        "recent_scores": []
                    }

                entry = local_stats_cache[sname]
                entry["count"] += 1
                
                # Parse last_played date
                last_played_dt = datetime.datetime.fromisoformat(entry["last_played"])
                if dt > last_played_dt:
                    entry["last_played"] = dt.isoformat()

                # Read score from file
                score_val = None
                try:
                    with open(os.path.join(stats_dir, fname), "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if line.startswith("Score:,"):
                                score_val = float(line.split(",")[1])
                                break
                except Exception:
                    pass

                if score_val is not None:
                    # Load existing recent scores, parse their dates to datetime
                    scores = []
                    for d_str, val in entry.get("recent_scores", []):
                        try:
                            scores.append((datetime.datetime.fromisoformat(d_str), val))
                        except ValueError:
                            pass
                    scores.append((dt, score_val))
                    # Sort and keep 10 most recent
                    scores = sorted(scores, key=lambda x: x[0])[-10:]
                    trend, runs_since_pb = _compute_trend_and_pb(scores)
                    entry["trend"] = trend
                    entry["runs_since_recent_pb"] = runs_since_pb
                    entry["recent_scores"] = [[s[0].isoformat(), s[1]] for s in scores]

            # Update cache dict known files list
            cache_dict["known_stat_files"] = list(current_files)

        # Prepare the stats output from the cache
        stats = {}
        for sname, entry in local_stats_cache.items():
            try:
                last_played = datetime.datetime.fromisoformat(entry["last_played"])
            except ValueError:
                last_played = now_dt
            stats[sname] = {
                "count": entry["count"],
                "last_played": last_played,
                "trend": entry.get("trend", 1.0),
                "runs_since_recent_pb": entry.get("runs_since_recent_pb", 0),
                "runs_today": 0
            }

    else:
        # Fallback to full parsing (for existing unit tests)
        stats = {}
        for fname in sorted(current_files, reverse=True):
            parts = fname[:-10].rsplit(" - ", 2)
            if len(parts) < 3:
                continue
            sname, _, date_str = parts
            try:
                dt = datetime.datetime.strptime(date_str, "%Y.%m.%d-%H.%M.%S")
            except ValueError:
                continue

            data = stats.setdefault(sname, {"count": 0, "last_played": dt, "recent_scores": [], "runs_today": 0})
            data["count"] += 1
            if dt > data["last_played"]:
                data["last_played"] = dt

            if len(data["recent_scores"]) < 10:
                try:
                    with open(os.path.join(stats_dir, fname), "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if line.startswith("Score:,"):
                                data["recent_scores"].append((dt, float(line.split(",")[1])))
                                break
                except Exception:
                    pass

        # Calculate trends
        for data in stats.values():
            scores = sorted(data.pop("recent_scores"), key=lambda x: x[0])
            trend, runs_since_pb = _compute_trend_and_pb(scores)
            data["trend"] = trend
            data["runs_since_recent_pb"] = runs_since_pb

    # Calculate runs_today dynamically from filenames for both paths (very fast!)
    yesterday = now_dt - datetime.timedelta(days=1)
    today_str = now_dt.strftime("%Y.%m.%d")
    yesterday_str = yesterday.strftime("%Y.%m.%d")

    for fname in current_files:
        if (today_str in fname or yesterday_str in fname) and len(parts := fname[:-10].rsplit(" - ", 2)) >= 3:
            sname, _, date_str = parts
            try:
                dt = datetime.datetime.strptime(date_str, "%Y.%m.%d-%H.%M.%S")
                if (now_dt - dt).total_seconds() < 86400 and sname in stats:
                    stats[sname]["runs_today"] += 1
            except ValueError:
                pass

    return stats
