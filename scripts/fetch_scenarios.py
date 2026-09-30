#!/usr/bin/env python3
import json
import time
import concurrent.futures
import logging
import os
import sys
import gzip
import datetime
import argparse
import tempfile
from pathlib import Path

import requests

# Add parent directory to path for module imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kovaaks.api import api_request_with_retry, get_accurate_entry_count
from kovaaks.data_processing import safe_int

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("fetch_scenarios")

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"
MAX_HISTORY = 168


def fetch_all_scenarios(pages_limit=0, entries_limit=100, existing_scenarios=None):
    url = "https://kovaaks.com/webapp-backend/scenario/popular"
    all_data = []
    page = 0
    session = requests.Session()
    
    # Increase connection pool size to match max_workers in ThreadPoolExecutor
    adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    # Single executor for the entire fetch (perf: was per-page before)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=20)
    try:
        while True:
            if pages_limit > 0 and page >= pages_limit:
                logger.info(f"Reached page limit of {pages_limit}")
                break

            logger.info(f"Fetching page {page}")
            params = {"page": page, "max": 100}
            try:
                resp = api_request_with_retry("get", url, params=params, session=session)
                if resp is None:
                    raise RuntimeError("request returned no response")
                data = resp.json()
            except Exception as e:
                logger.error(f"Failed to fetch page {page}: {e}")
                raise RuntimeError(f"Failed to fetch scenario page {page}") from e
                
            items = data.get("data", [])
            if not items:
                break

            # Check if we should stop based on the original API counts (before accurate overwrite)
            max_on_page = max(
                (safe_int(it.get("counts", {}).get("entries", 0)) for it in items),
                default=0,
            )
            should_stop = max_on_page < entries_limit

            # Fetch accurate entry counts in parallel for the current page
            future_to_item = {
                executor.submit(get_accurate_entry_count, it.get("leaderboardId"), session): it
                for it in items
            }
            for future in concurrent.futures.as_completed(future_to_item):
                item = future_to_item[future]
                accurate_count = future.result()
                
                if accurate_count is None and existing_scenarios:
                    old_item = existing_scenarios.get(item.get("leaderboardId"))
                    if old_item:
                        accurate_count = safe_int(old_item.get("counts", {}).get("entries", 0))
                            
                if accurate_count is None:
                    accurate_count = 0
                    
                if "counts" not in item:
                    item["counts"] = {}
                item["counts"]["entries"] = accurate_count
                if "scenario" in item and "counts" in item["scenario"]:
                    item["scenario"]["counts"]["entries"] = accurate_count

            all_data.extend(items)
            
            if should_stop:
                logger.info(f"Stopping at page {page} - max entries {max_on_page} < {entries_limit}")
                break

            total = safe_int(data.get("total", 0))
            page += 1
            if len(all_data) >= total:
                break
            
            # Respectful delay
            time.sleep(0.2)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        session.close()

    logger.info(f"Fetched {len(all_data)} total scenarios")
    return all_data


def load_scenarios(path):
    """Load scenario data, aborting on unreadable or malformed existing files."""
    path = Path(path)
    if not path.exists():
        return {}
    # Load existing scenarios if file exists
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("counts", {}), dict)
        for item in data
    ):
        raise ValueError(f"Invalid scenario dataset: {path}")
    # Use leaderboardId as key for deduplication
    existing = {item["leaderboardId"]: item for item in data if item.get("leaderboardId")}
    logger.info("Loaded %s existing scenarios from %s", len(existing), path)
    return existing


def load_history(path):
    """Load history before modifying either output, preserving corrupt sources."""
    path = Path(path)
    if not path.exists():
        return {"timestamps": [], "history": {}}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("timestamps"), list)
        or not all(isinstance(stamp, str) for stamp in data["timestamps"])
        or not isinstance(data.get("history"), dict)
        or any(
            not isinstance(counts, list) or len(counts) > len(data["timestamps"])
            for counts in data["history"].values()
        )
    ):
        raise ValueError(f"Invalid history dataset: {path}")
    for stamp in data["timestamps"]:
        try:
            datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid history timestamp in {path}: {stamp}") from exc
    logger.info("Loaded history with %s timestamps", len(data["timestamps"]))
    return data


def merge_scenarios(existing_scenarios, new_scenarios):
    """Merge fetched scenarios with retained data and produce a stable ordering."""
    # Merge new into existing (new overwrites old for same leaderboardId)
    merged = dict(existing_scenarios)
    for scenario in new_scenarios:
        leaderboard_id = scenario.get("leaderboardId")
        if leaderboard_id:
            merged[leaderboard_id] = scenario
    # Convert back to list and clean up
    # Sort by entries count descending; equal counts have a stable ID order.
    return sorted(
        merged.values(),
        key=lambda item: (-safe_int(item.get("counts", {}).get("entries")), str(item["leaderboardId"])),
    )


def update_history(history_data, scenarios, now=None):
    """Update the current hourly sample and retain the last seven days of history."""
    # --- History Tracking ---
    # Add current timestamp
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    timestamps = list(history_data["timestamps"])
    current_history = {lid: list(counts) for lid, counts in history_data["history"].items()}
    replace_latest = False
    if timestamps:
        try:
            last_dt = datetime.datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
            if last_dt.tzinfo is not None:
                last_dt = last_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            replace_latest = 0 <= (now - last_dt).total_seconds() < 3600
        except ValueError:
            pass

    if not replace_latest:
        timestamps.append(now.isoformat())
    # Ensure all existing LIDs also get a value (None if not in current fetch)
    for counts in current_history.values():
        counts.extend([None] * (len(timestamps) - len(counts)))

    # Update history for all merged scenarios
    for scenario in scenarios:
        lid = str(scenario["leaderboardId"])
        if lid not in current_history:
            # Initialize with nulls for past timestamps to keep alignment
            current_history[lid] = [None] * len(timestamps)
        current_history[lid][-1] = safe_int(scenario.get("counts", {}).get("entries"))

    # Prune to last 168 records (7 days if 1 point per hour)
    return {
        "timestamps": timestamps[-MAX_HISTORY:],
        "history": {lid: counts[-MAX_HISTORY:] for lid, counts in current_history.items()},
    }


def write_gzip_json(path, data):
    """Atomically write deterministic compressed JSON unless its content is unchanged."""
    path = Path(path)
    serialized = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if path.exists():
        # Do not rewrite files whose logical content did not change.
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            if json.load(stream) == data:
                return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp", delete=False
        ) as stream:
            temporary_path = Path(stream.name)
            # Suppress temporary filenames and timestamps in the gzip header.
            with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as compressed:
                compressed.write(serialized)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return True


def generate_datasets(output_dir=DEFAULT_OUTPUT_DIR, pages_limit=0, entries_limit=1000, now=None):
    """Fetch and merge release datasets, retaining prior history in output_dir."""
    output_dir = Path(output_dir)
    scenarios_path = output_dir / "scenarios.json.gz"
    history_path = output_dir / "scenarios_history.json.gz"
    # Validate both existing files before fetching or writing either output.
    existing_scenarios = load_scenarios(scenarios_path)
    history_data = load_history(history_path)

    # Fetch new scenarios
    new_scenarios = fetch_all_scenarios(
        pages_limit=pages_limit, entries_limit=entries_limit, existing_scenarios=existing_scenarios
    )
    if not new_scenarios or not any(item.get("leaderboardId") for item in new_scenarios):
        raise RuntimeError("No scenarios fetched; refusing to publish stale or empty datasets")
    merged_list = merge_scenarios(existing_scenarios, new_scenarios)
    history_data = update_history(history_data, merged_list, now=now)

    scenarios_changed = write_gzip_json(scenarios_path, merged_list)
    logger.info("Merged %s total scenarios into %s (changed=%s)", len(merged_list), scenarios_path, scenarios_changed)
    # Save compressed history
    history_changed = write_gzip_json(history_path, history_data)
    logger.info("Saved compressed history to %s (changed=%s)", history_path, history_changed)
    return scenarios_changed or history_changed


def main(argv=None):
    """Command-line entry point; failed fetches never count as successful publication."""
    parser = argparse.ArgumentParser(description="Fetch KovaaKs scenarios and update release datasets.")
    parser.add_argument("--pages", type=int, default=0, help="Number of pages to fetch (0 for all)")
    parser.add_argument("--min-entries", type=int, default=1000, help="Stop fetching when max entries on a page falls below this")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Writable dataset directory (default: %(default)s)")
    args = parser.parse_args(argv)
    if args.pages < 0 or args.min_entries < 0:
        parser.error("--pages and --min-entries must be nonnegative")
    try:
        generate_datasets(args.output_dir, pages_limit=args.pages, entries_limit=args.min_entries)
    except Exception as exc:
        logger.error("Error in fetch script: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
