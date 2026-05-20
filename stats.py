"""
KovaaKs Scenario Tracker — local stats parsing.

Reads KovaaK's stats CSV files from the Steam directory to extract
play counts, recency, trends, and score history.
"""

import datetime
import logging
import os

logger = logging.getLogger("kovaaks")


def get_local_stats(stats_dir):
    """Extract local scenario stats (counts, recency, trends) from the Steam stats directory."""
    stats = {}
    if not os.path.exists(stats_dir):
        logger.warning("Stats directory not found: %s", stats_dir)
        return stats

    try:
        now_dt = datetime.datetime.now()
        for fname in sorted(os.listdir(stats_dir), reverse=True):
            if not fname.endswith(" Stats.csv"):
                continue
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
            if (now_dt - dt).total_seconds() < 86400:
                data["runs_today"] += 1
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
            trend, runs_since_pb = 1.0, 0
            if len(scores) >= 2:
                max_score = max(s[1] for s in scores)
                runs_since_pb = len(scores) - 1 - max(i for i, s in enumerate(scores) if s[1] == max_score)
                if runs_since_pb == len(scores) - 1:
                    runs_since_pb = 999
                avg_change = (scores[-1][1] - scores[0][1]) / (len(scores) - 1)
                if max_score > 1.0:
                    trend = max(0.5, min(2.0, 1.0 + (avg_change / max_score) * 5.0))
            data["trend"] = trend
            data["runs_since_recent_pb"] = runs_since_pb

    except Exception as e:
        logger.error("Error reading local stats: %s", e)

    return stats
