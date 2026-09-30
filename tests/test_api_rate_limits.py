"""HTTP retry integration with isolated pacing clocks and no network access."""

from email.utils import formatdate
import logging

import pytest
import requests

from kovaaks import api


FRIENDS_URL = "https://kovaaks.com/webapp-backend/leaderboard/scores/friends"
GLOBAL_URL = "https://kovaaks.com/webapp-backend/leaderboard/scores/global"


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.epoch = 1_800_000_000.0
        self.sleeps = []
        self.on_sleep = None

    def monotonic(self):
        return self.now

    def wall_clock(self):
        return self.epoch + self.now

    def sleep(self, duration):
        assert duration >= 0
        self.sleeps.append(duration)
        self.now += duration
        if self.on_sleep:
            self.on_sleep()


class FakeSession:
    def __init__(self, clock, responses):
        self.clock = clock
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((self.clock.now, url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def response(status, retry_after=None):
    result = requests.Response()
    result.status_code = status
    result.url = FRIENDS_URL
    result._content = b'{"data": [], "total": 0}'
    result._content_consumed = True
    if retry_after is not None:
        result.headers["Retry-After"] = retry_after
    return result


@pytest.fixture
def pacing(monkeypatch):
    clock = FakeClock()
    pacer = api.RequestPacer(clock=clock.monotonic, sleep=clock.sleep, wall_clock=clock.wall_clock)
    monkeypatch.setattr(api, "_KOVAAKS_PACER", pacer)
    # Cover unpaced/error backoff paths as well: no test may actually sleep.
    monkeypatch.setattr(api.time, "sleep", clock.sleep)
    return clock


@pytest.mark.parametrize("header_kind", ["seconds", "http_date"])
def test_retry_after_defers_retry_and_avoids_duplicate_error_logs(pacing, caplog, header_kind):
    hint = "37" if header_kind == "seconds" else formatdate(pacing.wall_clock() + 37, usegmt=True)
    session = FakeSession(pacing, [response(429, hint), response(200)])

    result = api.api_request_with_retry("get", FRIENDS_URL, session=session, max_retries=1)

    assert result.status_code == 200
    assert len(session.calls) == 2
    assert session.calls[1][0] - session.calls[0][0] >= 37
    warnings = [record.getMessage() for record in caplog.records
                if record.name == "kovaaks" and record.levelno >= logging.WARNING]
    assert not any("Connection error" in message for message in warnings)
    assert not any("Server error/timeout" in message for message in warnings)


def test_exhausted_rate_limit_still_cools_down_other_session_and_endpoint(pacing):
    limited = FakeSession(pacing, [response(429, "41")])
    other = FakeSession(pacing, [response(200)])

    with pytest.raises(requests.HTTPError):
        api.api_request_with_retry("get", FRIENDS_URL, session=limited, max_retries=0)
    result = api.api_request_with_retry("get", GLOBAL_URL, session=other, max_retries=0)

    assert result.status_code == 200
    assert len(limited.calls) == 1
    assert len(other.calls) == 1
    assert other.calls[0][0] - limited.calls[0][0] >= 41


def test_exhausted_rate_limit_retries_are_bounded(pacing):
    session = FakeSession(pacing, [response(429, "2") for _ in range(3)])

    with pytest.raises(requests.HTTPError):
        api.api_request_with_retry("get", FRIENDS_URL, session=session, max_retries=2)

    assert len(session.calls) == 3
    assert all(later[0] - earlier[0] >= 2
               for earlier, later in zip(session.calls, session.calls[1:]))


@pytest.mark.parametrize("status", [401, 403, 404])
def test_other_client_errors_raise_without_retry(pacing, status):
    session = FakeSession(pacing, [response(status)])

    with pytest.raises(requests.HTTPError):
        api.api_request_with_retry("get", FRIENDS_URL, session=session, max_retries=3)

    assert len(session.calls) == 1
    assert pacing.sleeps == []


@pytest.mark.parametrize("other_url", [
    "https://github.com/example/data/releases/download/latest/scenarios.json.gz",
    "https://kovaaks.com.example.org/path",
])
def test_cooldown_does_not_delay_other_hosts(pacing, other_url):
    limited = FakeSession(pacing, [response(429, "30")])
    other = FakeSession(pacing, [response(200)])
    with pytest.raises(requests.HTTPError):
        api.api_request_with_retry("get", FRIENDS_URL, session=limited, max_retries=0)
    before = pacing.now

    assert api.api_request_with_retry("get", other_url, session=other).status_code == 200

    assert other.calls[0][0] == before


def test_cancellation_before_first_attempt_sends_no_request(pacing):
    session = FakeSession(pacing, [response(200)])

    with pytest.raises(api.RequestCancelled):
        api.api_request_with_retry("get", FRIENDS_URL, session=session, cancel_check=lambda: True)

    assert session.calls == []
    assert pacing.sleeps == []


def test_cancellation_during_spacing_sends_no_next_request(pacing):
    session = FakeSession(pacing, [response(200), response(200)])
    api.api_request_with_retry("get", FRIENDS_URL, session=session)
    cancelled = False

    def cancel():
        nonlocal cancelled
        cancelled = True

    pacing.on_sleep = cancel

    with pytest.raises(api.RequestCancelled):
        api.api_request_with_retry("get", GLOBAL_URL, session=session, cancel_check=lambda: cancelled)

    assert len(session.calls) == 1


def test_cancellation_during_rate_limit_backoff_does_not_retry(pacing):
    session = FakeSession(pacing, [response(429, "20"), response(200)])
    cancelled = False

    def cancel():
        nonlocal cancelled
        cancelled = True

    pacing.on_sleep = cancel

    with pytest.raises(api.RequestCancelled):
        api.api_request_with_retry("get", FRIENDS_URL, session=session,
                                   cancel_check=lambda: cancelled, max_retries=2)

    assert len(session.calls) == 1


@pytest.mark.parametrize("failure", ["server_error", "connection_error"])
def test_cancellation_interrupts_ordinary_retry_backoff(pacing, failure):
    initial = response(500) if failure == "server_error" else requests.ConnectionError("Offline")
    session = FakeSession(pacing, [initial, response(200)])
    cancelled = False

    def cancel():
        nonlocal cancelled
        cancelled = True

    pacing.on_sleep = cancel

    with pytest.raises(api.RequestCancelled):
        api.api_request_with_retry("get", FRIENDS_URL, session=session,
                                   cancel_check=lambda: cancelled, max_retries=2)

    assert len(session.calls) == 1
    assert pacing.now < 2


def test_transport_raised_rate_limit_also_publishes_shared_cooldown(pacing):
    error = requests.HTTPError(response=response(429, "41"))
    session = FakeSession(pacing, [error, response(200)])

    assert api.api_request_with_retry("get", FRIENDS_URL, session=session, max_retries=1).status_code == 200

    assert len(session.calls) == 2
    assert session.calls[1][0] - session.calls[0][0] >= 41


def test_cancellation_callback_exception_is_preserved(pacing):
    session = FakeSession(pacing, [response(200)])

    def check():
        raise RuntimeError("Caller-specific cancellation")

    with pytest.raises(RuntimeError, match="Caller-specific cancellation"):
        api.api_request_with_retry("get", FRIENDS_URL, session=session, cancel_check=check)

    assert session.calls == []


@pytest.mark.parametrize("helper", ["friends", "accurate_count"])
def test_cancelled_helpers_do_not_return_authoritative_empty_data(pacing, helper):
    session = FakeSession(pacing, [response(200)])

    with pytest.raises(api.RequestCancelled):
        if helper == "friends":
            api.kovaaks_get_friends_scores("token", "one", session=session, cancel_check=lambda: True)
        else:
            api.get_accurate_entry_count("one", session=session, cancel_check=lambda: True)

    assert session.calls == []
