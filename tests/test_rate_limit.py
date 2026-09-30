"""Deterministic pacing tests: no live requests, real sleeps, or cache files."""

from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate

import pytest

from kovaaks.rate_limit import RequestPacer, parse_retry_after


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now
        self.sleeps = []
        self.after_sleep = None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.after_sleep is not None:
            self.after_sleep()


@pytest.fixture
def paced():
    clock = FakeClock()
    return RequestPacer(clock=clock, sleep=clock.sleep, wall_clock=lambda: 1_700_000_000), clock


@pytest.mark.parametrize("value,expected", [("12", 12), (2.5, 2.5), ("0", 0),
                                            (None, None), (True, None), ("-1", None),
                                            ("garbage", None), ("NaN", None), ("inf", None)])
def test_retry_after_seconds_and_invalid_headers(value, expected):
    assert parse_retry_after(value, now=1_700_000_000) == expected


def test_retry_after_http_dates_use_wall_clock_and_clamp_past_dates():
    now = 1_700_000_000
    assert parse_retry_after(formatdate(now + 90, usegmt=True), now=now) == 90
    assert parse_retry_after(formatdate(now - 90, usegmt=True), now=now) == 0


def test_requests_are_spaced_without_accumulating_idle_credits(paced):
    pacer, clock = paced
    assert pacer.wait()
    assert clock.now == 0
    assert pacer.wait()
    assert clock.now == 0.25

    clock.now = 10
    assert pacer.acquire()
    assert clock.now == 10
    assert pacer.wait()
    assert clock.now == 10.25


def test_waiter_observes_cooldown_imposed_by_another_request(paced):
    pacer, clock = paced
    assert pacer.wait()

    def extend_cooldown():
        clock.after_sleep = None
        pacer.record_rate_limit("30")

    clock.after_sleep = extend_cooldown
    assert pacer.wait()

    # A worker waiting only for normal spacing must notice the new 30s cooldown.
    assert clock.now == pytest.approx(30.25)
    assert max(clock.sleeps) <= 1


def test_cooldown_extension_is_observed_while_already_waiting(paced):
    pacer, clock = paced
    pacer.record_rate_limit()

    def extend_cooldown():
        clock.after_sleep = None
        pacer.record_rate_limit("45")

    clock.after_sleep = extend_cooldown
    assert pacer.wait()
    assert clock.now == 46


def test_cancellation_interrupts_long_cooldown_without_consuming_admission(paced):
    pacer, clock = paced
    pacer.record_rate_limit("600")

    assert pacer.wait(cancel_check=lambda: clock.now >= 0.2) is False
    assert clock.now == pytest.approx(0.2)
    assert max(clock.sleeps) <= 0.1

    clock.now = 600
    assert pacer.wait()
    assert clock.now == 600


def test_initial_cancellation_returns_without_sleeping(paced):
    pacer, clock = paced
    assert pacer.wait(cancel_check=lambda: True) is False
    assert clock.sleeps == []
    assert pacer.wait()
    assert clock.now == 0


def test_cancellation_exceptions_propagate(paced):
    pacer, clock = paced

    def cancelled():
        raise RuntimeError("Fetch cancelled")

    with pytest.raises(RuntimeError, match="Fetch cancelled"):
        pacer.wait(cancel_check=cancelled)
    assert clock.sleeps == []


def test_concurrent_429s_slow_pacing_only_once_per_wave(paced):
    pacer, clock = paced
    # The fake clock remains fixed while threads concurrently report failures.
    with ThreadPoolExecutor(max_workers=16) as executor:
        delays = list(executor.map(pacer.record_rate_limit, [None] * 64))

    assert delays == [15] * 64
    assert pacer.wait()
    assert clock.now == 15
    assert pacer.wait()
    assert clock.now == 15.5


def test_distinct_429_waves_back_off_with_bounded_interval_and_default_delay(paced):
    pacer, clock = paced
    expected = [(15, 0.5), (30, 1), (60, 2), (120, 4), (120, 4)]
    for cooldown, spacing in expected:
        started = clock.now
        assert pacer.record_rate_limit() == cooldown
        assert pacer.wait()
        assert clock.now == started + cooldown
        assert pacer.wait()
        assert clock.now == started + cooldown + spacing


@pytest.mark.parametrize("header", ["300", formatdate(1_700_000_300, usegmt=True)])
def test_server_retry_after_can_exceed_default_backoff_ceiling(paced, header):
    pacer, clock = paced
    assert pacer.record_rate_limit(header) == 300
    assert pacer.wait()
    assert clock.now == 300


def test_short_retry_after_does_not_allow_an_immediate_retry_wave(paced):
    pacer, clock = paced
    assert pacer.on_rate_limit("0") == 15
    assert pacer.record_rate_limit("1") == 15
    assert pacer.wait()
    assert clock.now == 15
    assert pacer.wait()
    assert clock.now == 15.5


def test_late_successes_do_not_erase_cooldown_or_count_towards_recovery(paced):
    pacer, clock = paced
    pacer.record_rate_limit()
    for _ in range(100):
        pacer.record_success()

    assert pacer.wait()
    assert clock.now == 15
    clock.now = 60
    pacer.record_success()
    assert pacer.wait()
    assert pacer.wait()
    assert clock.now == 60.5


def test_recovery_requires_both_sustained_success_and_elapsed_time(paced):
    pacer, clock = paced
    pacer.record_rate_limit()
    clock.now = 15
    for _ in range(50):
        pacer.record_success()
    assert pacer.wait()
    assert pacer.wait()
    assert clock.now == 15.5

    clock.now = 60
    pacer.record_success()
    assert pacer.wait()
    assert pacer.wait()
    assert clock.now == pytest.approx(60.4)

    # Another rapid streak cannot immediately restore the original spacing.
    for _ in range(50):
        pacer.record_success()
    assert pacer.wait()
    assert clock.now == pytest.approx(60.8)


def test_recovery_never_exceeds_original_request_rate(paced):
    pacer, clock = paced
    pacer.record_rate_limit()
    for epoch in range(1, 11):
        clock.now = epoch * 60
        for _ in range(50):
            pacer.record_success()

    assert pacer.wait()
    assert pacer.wait()
    assert clock.now == pytest.approx(600.25)


@pytest.mark.parametrize("kwargs", [
    {"interval": 0}, {"interval": float("nan")}, {"cooldown_default": -1},
    {"max_interval": float("inf")}, {"interval": 1, "max_interval": 0.5},
])
def test_invalid_pacing_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        RequestPacer(**kwargs)
