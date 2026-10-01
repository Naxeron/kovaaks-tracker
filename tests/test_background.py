"""Background request pools must never keep a closed application alive."""

from concurrent.futures import as_completed
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from kovaaks.background import DaemonThreadPoolExecutor


def test_pool_returns_results_and_worker_survives_failure():
    def fail():
        raise ValueError("request failed")

    with DaemonThreadPoolExecutor(max_workers=1) as executor:
        failed = executor.submit(fail)
        result = executor.submit(lambda value, *, extra: value + extra, 10, extra=2)
        with pytest.raises(ValueError, match="request failed"):
            failed.result(timeout=2)
        assert result.result(timeout=2) == 12

    assert all(not worker.is_alive() for worker in executor._threads)
    with pytest.raises(RuntimeError, match="after shutdown"):
        executor.submit(lambda: None)


def test_shutdown_cancels_queued_work_without_waiting_for_blocked_request():
    entered = threading.Event()
    release = threading.Event()
    executed = []

    def blocked_request():
        entered.set()
        assert release.wait(timeout=3), "Test did not release the blocked request"
        return "finished"

    executor = DaemonThreadPoolExecutor(max_workers=1)
    running = executor.submit(blocked_request)
    try:
        assert entered.wait(timeout=2)
        queued = executor.submit(executed.append, "should not run")
        executor.shutdown(wait=False, cancel_futures=True)

        assert not running.done()
        assert queued.cancelled()
        assert list(as_completed([queued], timeout=1)) == [queued]
        with pytest.raises(RuntimeError, match="after shutdown"):
            executor.submit(lambda: None)
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)

    assert running.result(timeout=2) == "finished"
    assert executed == []
    assert all(not worker.is_alive() for worker in executor._threads)


def test_nonblocking_shutdown_finishes_accepted_jobs():
    executed = []
    executor = DaemonThreadPoolExecutor(max_workers=2)
    jobs = [executor.submit(executed.append, value) for value in range(10)]
    executor.shutdown(wait=False)
    executor.shutdown(wait=True)

    assert all(job.done() for job in jobs)
    assert sorted(executed) == list(range(10))
    assert all(not worker.is_alive() for worker in executor._threads)


def test_cancelling_an_individual_job_skips_it():
    entered = threading.Event()
    release = threading.Event()
    executor = DaemonThreadPoolExecutor(max_workers=1)
    executed = []

    def block():
        entered.set()
        assert release.wait(timeout=3)

    executor.submit(block)
    try:
        assert entered.wait(timeout=2)
        future = executor.submit(executed.append, "cancelled")
        assert future.cancel()
    finally:
        release.set()
        executor.shutdown(wait=True)

    assert executed == []
    assert list(as_completed([future], timeout=1)) == [future]


@pytest.mark.parametrize("max_workers", [0, -1])
def test_pool_rejects_nonpositive_worker_count(max_workers):
    with pytest.raises(ValueError, match="greater than 0"):
        DaemonThreadPoolExecutor(max_workers=max_workers)


def test_blocked_request_does_not_pin_interpreter_shutdown():
    """A thread marked daemon is insufficient if an executor exit hook joins it."""
    script = """
import threading
from kovaaks.background import DaemonThreadPoolExecutor

entered = threading.Event()
def blocked():
    entered.set()
    threading.Event().wait()

executor = DaemonThreadPoolExecutor(max_workers=1)
executor.submit(blocked)
assert entered.wait(timeout=2)
executor.shutdown(wait=False, cancel_futures=True)
print('closed', flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "closed"
