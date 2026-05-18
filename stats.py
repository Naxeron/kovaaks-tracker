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
        filenames = os.listdir(stats_dir)
        # Sort filenames by date (descending) so we process most recent first
        # Pattern: ... - 2026.01.11-15.13.08 Stats.csv
        filenames.sort(reverse=True)

        for fname in filenames:
            if fname.endswith(" Stats.csv"):
                # Pattern: [Scenario Name] - [Mode] - [Date] Stats.csv
                base = fname[:-10]
                parts = base.rsplit(" - ", 2)
                if len(parts) >= 3:
                    sname = parts[0]
                    date_str = parts[2]
                    try:
                        dt = datetime.datetime.strptime(date_str, "%Y.%m.%d-%H.%M.%S")
                    except ValueError:
                        continue

                    if sname not in stats:
                        stats[sname] = {"count": 0, "last_played": dt,
                                        "recent_scores": [], "runs_today": 0}

                    stats[sname]["count"] += 1

                    if (now_dt - dt).total_seconds() < 86400:
                        stats[sname]["runs_today"] += 1
                    if dt > stats[sname]["last_played"]:
                        stats[sname]["last_played"] = dt

                    # Store most recent scores for trend (already sorted by filenames.sort)
                    if len(stats[sname]["recent_scores"]) < 10:
                        fpath = os.path.join(stats_dir, fname)
                        try:
                            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                                for line in f:
                                    if line.startswith("Score:,"):
                                        score_val = float(line.split(",")[1])
                                        stats[sname]["recent_scores"].append((dt, score_val))
                                        break
                        except Exception:
                            pass

        # Calculate trends
        for sname, data in stats.items():
            # filenames were reversed, so scores are newest first. Reverse back for trend.
            scores = sorted(data["recent_scores"], key=lambda x: x[0])
            if len(scores) >= 2:
                changes = [scores[i][1] - scores[i-1][1] for i in range(1, len(scores))]
                avg_change = sum(changes) / len(changes)
                max_score = max(s[1] for s in scores)

                runs_since_pb = 0
                for i in range(len(scores)-1, -1, -1):
                    if scores[i][1] == max_score:
                        runs_since_pb = len(scores) - 1 - i
                        break
                if runs_since_pb == len(scores) - 1:
                    runs_since_pb = 999
                data["runs_since_recent_pb"] = runs_since_pb

                if max_score > 1.0:  # avoid division by zero or tiny scores
                    # Trend factor: 1.0 is neutral.
                    # If improving by 1% of max score per run, factor is 1.05
                    data["trend"] = max(0.5, min(2.0,
                        1.0 + (avg_change / max_score) * 5.0))
                else:
                    data["trend"] = 1.0
            else:
                data["trend"] = 1.0
                data["runs_since_recent_pb"] = 0
            del data["recent_scores"]

    except Exception as e:
        logger.error("Error reading local stats: %s", e)

    return stats
