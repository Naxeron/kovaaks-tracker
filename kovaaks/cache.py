"""
KovaaKs Scenario Tracker — cache utilities.

Handles loading and saving the gzip-compressed JSON scores cache.
"""

import base64
import gzip
import json
import logging
import os
import threading
import zlib

from .history import CompactHistory

logger = logging.getLogger("kovaaks")

PROJECT_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCORES_CACHE = os.path.join(PROJECT_DIR, "data", "scores_cache.json.gz")

_cache_lock = threading.Lock()
_HISTORY_FORMAT = "packed-history-v1"


class _PackedHistoryRow:
    """Defer base64 encoding until JSON writes this individual scenario."""

    __slots__ = ("timeline", "counts")

    def __init__(self, timeline, counts):
        self.timeline = timeline
        self.counts = counts


class _CacheEncoder(json.JSONEncoder):
    def default(self, value):
        if isinstance(value, _PackedHistoryRow):
            return [value.timeline, base64.b64encode(value.counts).decode("ascii")]
        if isinstance(value, CompactHistory):
            return dict(value.items())
        return super().default(value)


def _encode_history(cache):
    """Share timestamps on disk and stream packed counts without a second copy.

    The versioned envelope is local-cache-only; downloaded release datasets keep
    their existing format. Unusual legacy values remain ordinary JSON mappings.
    """
    history = cache.get("entry_history")
    if not isinstance(history, dict) or not history:
        return cache
    timelines, timeline_ids, series = [], {}, {}
    for lid, points in history.items():
        points = points if isinstance(points, CompactHistory) else CompactHistory(points)
        if not points.is_compact:
            series[lid] = points
            continue
        stamps = points.timestamps
        index = timeline_ids.get(stamps)
        if index is None:
            index = len(timelines)
            timeline_ids[stamps] = index
            timelines.append(stamps)
        series[lid] = _PackedHistoryRow(index, points.packed_counts)
    result = dict(cache)
    result["entry_history"] = {
        "_format": _HISTORY_FORMAT, "timelines": timelines, "series": series,
    }
    return result


def _decode_history(cache):
    """Accept legacy mappings and reject malformed packed caches without saving."""
    history = cache.get("entry_history")
    if history is None:
        return
    if not isinstance(history, dict):
        raise ValueError("entry_history must be an object")
    if isinstance(history.get("_format"), str):
        if history["_format"] != _HISTORY_FORMAT:
            raise ValueError("Unsupported entry history format")
        timelines, series = history.get("timelines"), history.get("series")
        if not isinstance(timelines, list) or not isinstance(series, dict):
            raise ValueError("Invalid packed history envelope")
        for stamps in timelines:
            if (not isinstance(stamps, list)
                    or any(not isinstance(stamp, str) for stamp in stamps)
                    or len(set(stamps)) != len(stamps)):
                raise ValueError("Invalid history timeline")
        for lid, row in series.items():
            if isinstance(row, dict):
                series[lid] = CompactHistory(row)
                continue
            if (not isinstance(row, list) or len(row) != 2
                    or type(row[0]) is not int or not 0 <= row[0] < len(timelines)
                    or not isinstance(row[1], str)):
                raise ValueError("Invalid packed history row")
            packed = base64.b64decode(row[1], validate=True)
            series[lid] = CompactHistory.from_packed(timelines[row[0]], packed)
        cache["entry_history"] = series
    else:
        # Replace one leaf at a time so migration does not keep two histories.
        for lid, points in history.items():
            if not isinstance(points, dict):
                raise ValueError("Invalid legacy history row")
            history[lid] = CompactHistory(points)


def load_scores_cache():
    """Load the unified gzip JSON cache from disk. Returns empty dict on failure."""
    if not os.path.exists(SCORES_CACHE):
        return {}
    try:
        with gzip.open(SCORES_CACHE, "rt", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("Could not load cache: expected a JSON object")
            return {}
        _decode_history(data)
        logger.info("Loaded cache from %s", SCORES_CACHE)
        return data
    except (ValueError, OSError, EOFError, UnicodeDecodeError, zlib.error) as e:
        logger.warning("Could not load cache: %s", e)
        return {}


def save_scores_cache(cache_dict):
    """Save a detached cache snapshot atomically, returning whether it succeeded."""
    with _cache_lock:
        tmp_cache = SCORES_CACHE + f".{os.getpid()}.tmp"
        try:
            with gzip.open(tmp_cache, "wt", encoding="utf-8", compresslevel=6) as f:
                json.dump(_encode_history(cache_dict), f, separators=(",", ":"), cls=_CacheEncoder)
            os.replace(tmp_cache, SCORES_CACHE)
            return True
        except (OSError, TypeError, ValueError) as e:
            logger.warning("Could not save cache: %s", e)
            return False
        finally:
            if os.path.exists(tmp_cache):
                try:
                    os.remove(tmp_cache)
                except OSError:
                    pass


class CacheWriter:
    """Serialize cache saves and combine requests arriving during a write.

    ``snapshot`` is called only when the writer is ready for another save. It
    must acquire the application's data lock and return detached data, so JSON
    encoding and compression never access the live cache. Callers must release
    that lock before requesting a blocking save. ``synchronous`` keeps the same
    behavior without a background thread for deterministic application tests.
    Returning None from ``snapshot`` skips the save, for example when a cache
    corruption safeguard has been activated since the request was queued.
    """

    def __init__(self, snapshot, save=None, synchronous=False):
        self._snapshot = snapshot
        self._save = save if save is not None else save_scores_cache
        self._synchronous = synchronous
        self._condition = threading.Condition()
        self._requested_generation = 0
        self._completed_generation = 0
        self._saved_generation = 0
        self._running = False

    def request(self, wait=False):
        """Queue a save, optionally waiting for this generation to finish.

        Nonblocking requests return True once queued. Blocking and synchronous
        requests return whether their generation, or a newer one, was saved.
        A failure releases waiters; another request retries the current data.
        """
        with self._condition:
            self._requested_generation += 1
            generation = self._requested_generation
            start_writer = not self._running
            if start_writer:
                self._running = True

        # Starting outside the condition also supports test thread doubles that
        # execute their target inline instead of creating an actual thread.
        if start_writer:
            if self._synchronous:
                self._drain()
            else:
                try:
                    threading.Thread(target=self._drain, daemon=True).start()
                except Exception:
                    logger.exception("Could not start cache writer")
                    with self._condition:
                        self._completed_generation = self._requested_generation
                        self._running = False
                        self._condition.notify_all()
                    return False

        if wait or self._synchronous:
            with self._condition:
                self._condition.wait_for(lambda: self._completed_generation >= generation)
                return self._saved_generation >= generation
        return True

    def flush(self):
        """Wait for already requested saves without creating another snapshot."""
        with self._condition:
            generation = self._requested_generation
            self._condition.wait_for(lambda: self._completed_generation >= generation)
            return self._saved_generation >= generation

    def _drain(self):
        """Take each snapshot after the preceding write, never out of order."""
        while True:
            with self._condition:
                generation = self._requested_generation
            saved = False
            interrupted = False
            try:
                snapshot = self._snapshot()
                # Older injected save functions return None on success.
                saved = snapshot is not None and self._save(snapshot) is not False
            except Exception:
                logger.exception("Could not persist cache snapshot")
            except BaseException:
                interrupted = True
                raise
            finally:
                # Release this potentially large copy before the next snapshot
                # callback allocates another one, including after failed saves.
                snapshot = None
                with self._condition:
                    self._completed_generation = generation
                    if saved:
                        self._saved_generation = generation
                    pending = self._requested_generation > generation
                    if interrupted:
                        self._completed_generation = self._requested_generation
                        pending = False
                    if not pending:
                        self._running = False
                    self._condition.notify_all()
            if not pending:
                return


def load_scenarios_from_cache(cache):
    """Extract cached scenario list from the unified cache dict."""
    scenarios = cache.get("scenarios", [])
    if scenarios:
        logger.debug("Loaded %d scenarios from JSON cache", len(scenarios))
    return scenarios
