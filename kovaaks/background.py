"""Small daemon worker pool for cancellable application background requests."""

from concurrent.futures import Executor, Future
import queue
import threading


def _run_worker(work_queue):
    """Drain accepted jobs, releasing references before waiting for more work."""
    while True:
        job = work_queue.get()
        if job is None:
            return
        future, function, args, kwargs = job
        try:
            if future.set_running_or_notify_cancel():
                try:
                    result = function(*args, **kwargs)
                except BaseException as error:
                    future.set_exception(error)
                else:
                    future.set_result(result)
                    result = None
        finally:
            job = future = function = args = kwargs = None


class DaemonThreadPoolExecutor(Executor):
    """Run I/O jobs without forcing interpreter shutdown to await blocked I/O.

    Unlike the standard thread executor, these workers have no interpreter-exit
    join hook. Owners must still cancel their operations and save accepted data
    before exiting; an unfinished network request may then be abandoned safely.
    Normal completion and ``shutdown(wait=True)`` wait for every accepted job.
    """

    def __init__(self, max_workers, thread_name_prefix="kovaaks-fetch"):
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._work_queue = queue.SimpleQueue()
        self._threads = []
        self._lock = threading.Lock()
        self._shutdown = False

    def submit(self, fn, /, *args, **kwargs):
        """Schedule one call and expose its result or exception as a Future."""
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if len(self._threads) < self._max_workers:
                worker = threading.Thread(
                    target=_run_worker,
                    args=(self._work_queue,),
                    name=f"{self._thread_name_prefix}-{len(self._threads)}",
                    daemon=True,
                )
                worker.start()
                self._threads.append(worker)
            future = Future()
            self._work_queue.put((future, fn, args, kwargs))
            return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        """Reject new jobs, optionally cancel queued calls, and stop idle workers."""
        cancelled_jobs = []
        with self._lock:
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        job = self._work_queue.get_nowait()
                    except queue.Empty:
                        break
                    if job is not None:
                        cancelled_jobs.append(job)
            # Repeated shutdown calls remain safe, including a later blocking
            # shutdown after the owner first requested nonblocking cancellation.
            for _ in self._threads:
                self._work_queue.put(None)
            threads = tuple(self._threads)
        # Future callbacks can call back into the executor, so cancel outside
        # its lock. Notify Future waiters just as a worker skipping a job would.
        for future, *_ in cancelled_jobs:
            if future.cancel():
                future.set_running_or_notify_cancel()
        if wait:
            for worker in threads:
                worker.join()
