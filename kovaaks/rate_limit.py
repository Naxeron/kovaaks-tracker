"""Shared pacing and cooldowns for requests to one remote service."""

import datetime
import math
import threading
import time
from email.utils import parsedate_to_datetime


def parse_retry_after(value, now=None):
    """Return a nonnegative delay for Retry-After seconds or an HTTP date.

    ``now`` is a Unix timestamp, independent of the monotonic clock used for
    scheduling. Invalid headers return None so callers can use their backoff.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            deadline = parsedate_to_datetime(str(value))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=datetime.timezone.utc)
            seconds = deadline.timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
        return max(0.0, seconds) if math.isfinite(seconds) else None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


class RequestPacer:
    """Admit requests without bursts and share rate-limit cooldowns across threads.

    The default four requests per second is conservative client tuning, not a
    published service quota. A 429 slows admission once per cooldown wave;
    subsequent 429s extend the shared deadline without repeatedly multiplying
    the interval. Retry-After is a minimum, including when it exceeds the normal
    exponential-backoff ceiling. Sustained success permits gradual recovery.

    Inject ``clock`` (monotonic), ``sleep``, and ``wall_clock`` for tests. Each
    service should share one instance across all its request workers.
    """

    def __init__(self, interval=0.25, cooldown_default=15, max_interval=4,
                 clock=None, sleep=None, wall_clock=None):
        interval = float(interval)
        cooldown_default = float(cooldown_default)
        max_interval = float(max_interval)
        if not all(math.isfinite(value) and value > 0
                   for value in (interval, cooldown_default, max_interval)):
            raise ValueError("Pacing intervals and cooldowns must be finite and positive")
        if max_interval < interval:
            raise ValueError("max_interval must be at least interval")
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._wall_clock = wall_clock if wall_clock is not None else time.time
        self._lock = threading.Lock()
        self._base_interval = interval
        self._interval = interval
        self._max_interval = max_interval
        self._cooldown_default = min(cooldown_default, 120.0)
        self._next_cooldown = self._cooldown_default
        self._wave_cooldown = self._cooldown_default
        now = self._clock()
        self._next_request_at = now
        self._cooldown_until = now
        self._last_rate_limit = now
        self._last_relaxation = now
        self._successes = 0

    def wait(self, cancel_check=None):
        """Wait for one admission; return False when cancellation returns True.

        Cancellation exceptions propagate unchanged. Deadlines are checked again
        after every sleep so another worker's 429 immediately affects waiters.
        A delayed worker receives no accumulated credits or catch-up burst.
        """
        sleep_limit = 0.1 if cancel_check is not None else 1.0
        while True:
            if cancel_check is not None and cancel_check() is True:
                return False
            with self._lock:
                now = self._clock()
                delay = max(self._next_request_at, self._cooldown_until) - now
                if delay <= 0:
                    self._next_request_at = now + self._interval
                    return True
            self._sleep(min(delay, sleep_limit))

    acquire = wait

    def record_rate_limit(self, retry_after=None):
        """Apply a shared 429 cooldown and return its remaining delay in seconds."""
        hint = parse_retry_after(retry_after, now=self._wall_clock())
        with self._lock:
            now = self._clock()
            if now >= self._cooldown_until:
                self._interval = min(self._max_interval, self._interval * 2)
                self._wave_cooldown = self._next_cooldown
                self._next_cooldown = min(120.0, self._next_cooldown * 2)
            delay = max(self._wave_cooldown, hint if hint is not None else 0.0)
            self._cooldown_until = max(self._cooldown_until, now + delay)
            self._last_rate_limit = now
            self._successes = 0
            return self._cooldown_until - now

    on_rate_limit = record_rate_limit

    def record_success(self):
        """Relax pacing only after 50 successes and 60 seconds without a 429."""
        with self._lock:
            now = self._clock()
            # Responses already in flight must not erase or shorten a cooldown.
            if now < self._cooldown_until:
                return
            self._successes += 1
            if (self._successes >= 50
                    and now - max(self._last_rate_limit, self._last_relaxation) >= 60):
                self._interval = max(self._base_interval, self._interval * 0.8)
                self._next_cooldown = max(self._cooldown_default, self._next_cooldown / 2)
                self._successes = 0
                self._last_relaxation = now
