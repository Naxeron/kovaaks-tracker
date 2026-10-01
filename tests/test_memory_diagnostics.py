"""Memory logging is portable, bounded, and independent of application behavior."""

import ctypes
import logging
import subprocess
from types import SimpleNamespace
import weakref

import pytest

from kovaaks import memory
from kovaaks_web import KovaaksAPI


@pytest.fixture
def isolated_trim_cache():
    memory._get_malloc_trim.cache_clear()
    try:
        yield
    finally:
        memory._get_malloc_trim.cache_clear()


def test_release_unused_memory_resolves_native_function_once(monkeypatch, isolated_trim_cache):
    libraries = []
    pads = []

    def trim(pad):
        pads.append(pad)
        return 1

    def load_library(name):
        libraries.append(name)
        return SimpleNamespace(malloc_trim=trim)

    monkeypatch.setattr(memory.sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", load_library)

    assert memory.release_unused_memory() is True
    assert memory.release_unused_memory() is True
    assert libraries == [None]
    assert pads == [0, 0]
    assert trim.argtypes == [ctypes.c_size_t]
    assert trim.restype is ctypes.c_int


@pytest.mark.parametrize("platform", ["darwin", "win32", "freebsd"])
def test_release_unused_memory_skips_unsupported_platforms(monkeypatch, platform):
    calls = []
    monkeypatch.setattr(memory.sys, "platform", platform)
    monkeypatch.setattr(memory, "_get_malloc_trim", lambda: calls.append(True))

    assert memory.release_unused_memory() is False
    assert calls == []


@pytest.mark.parametrize("failure", ["library", "symbol"])
def test_unavailable_allocator_cleanup_is_cached(monkeypatch, isolated_trim_cache, failure):
    calls = []

    def load_library(name):
        calls.append(name)
        if failure == "library":
            raise OSError("Native library unavailable")
        return SimpleNamespace()

    monkeypatch.setattr(memory.sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", load_library)

    assert memory.release_unused_memory() is False
    assert memory.release_unused_memory() is False
    assert calls == [None]


@pytest.mark.parametrize("native_result", [0, RuntimeError("Native call failed")])
def test_allocator_cleanup_failure_never_interrupts_work(monkeypatch, native_result):
    def trim(pad):
        if isinstance(native_result, Exception):
            raise native_result
        return native_result

    monkeypatch.setattr(memory.sys, "platform", "linux")
    monkeypatch.setattr(memory, "_get_malloc_trim", lambda: trim)
    assert memory.release_unused_memory() is False


def test_linux_status_uses_current_and_peak_resident_memory(tmp_path):
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmSize:\t999999 kB\nVmRSS:\t2048 kB\nVmHWM:\t4096 kB\n")

    assert memory._read_linux_memory(status) == {
        "rss_bytes": 2 * 1024 * 1024, "peak_rss_bytes": 4 * 1024 * 1024,
    }


def test_linux_status_ignores_invalid_counters(tmp_path):
    status = tmp_path / "status"
    status.write_text("VmRSS: invalid kB\nVmRSS: -1 kB\nVmHWM: 4 unknown\n")
    assert memory._read_linux_memory(status) == {}


def test_windows_counters_use_native_working_set(monkeypatch):
    def current_process():
        return 123

    def process_info(handle, pointer, size):
        assert handle == 123
        counters = pointer._obj
        assert counters.cb == size == ctypes.sizeof(counters)
        counters.WorkingSetSize = 1024
        counters.PeakWorkingSetSize = 4096
        return 1

    libraries = {
        "kernel32": SimpleNamespace(GetCurrentProcess=current_process),
        "psapi": SimpleNamespace(GetProcessMemoryInfo=process_info),
    }
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: libraries[name], raising=False)

    assert memory._read_windows_memory() == {"rss_bytes": 1024, "peak_rss_bytes": 4096}


def test_ps_fallback_has_bounded_timeout_and_never_uses_shell(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="  2048\n")

    monkeypatch.setattr(memory.subprocess, "run", run)
    assert memory._read_ps_memory() == 2 * 1024 * 1024
    command, options = calls[0]
    assert command == ["ps", "-o", "rss=", "-p", str(memory.os.getpid())]
    assert options["timeout"] <= 0.5
    assert not options.get("shell", False)


def test_linux_metrics_do_not_spawn_ps(monkeypatch):
    monkeypatch.setattr(memory.sys, "platform", "linux")
    expected = {"rss_bytes": 1024, "peak_rss_bytes": 2048}
    monkeypatch.setattr(memory, "_read_linux_memory", lambda: expected)

    def unexpected():
        raise AssertionError("Native counters should avoid fallback work")

    monkeypatch.setattr(memory, "_read_ps_memory", unexpected)
    monkeypatch.setattr(memory, "_read_peak_memory", unexpected)
    assert memory.get_process_memory() == expected


def test_missing_native_counters_use_fallbacks(monkeypatch):
    monkeypatch.setattr(memory.sys, "platform", "linux")

    def missing():
        raise OSError("proc unavailable")

    monkeypatch.setattr(memory, "_read_linux_memory", missing)
    monkeypatch.setattr(memory, "_read_ps_memory", lambda: 1024)
    monkeypatch.setattr(memory, "_read_peak_memory", lambda: 2048)
    assert memory.get_process_memory() == {"rss_bytes": 1024, "peak_rss_bytes": 2048}


def test_failed_measurements_are_reported_as_unavailable(monkeypatch):
    monkeypatch.setattr(memory.sys, "platform", "linux")

    def unavailable():
        raise subprocess.TimeoutExpired("ps", 0.5)

    monkeypatch.setattr(memory, "_read_linux_memory", unavailable)
    monkeypatch.setattr(memory, "_read_ps_memory", unavailable)
    monkeypatch.setattr(memory, "_read_peak_memory", unavailable)
    assert memory.get_process_memory() == {"rss_bytes": None, "peak_rss_bytes": None}


def test_log_memory_distinguishes_current_usage_from_peak(monkeypatch, caplog):
    monkeypatch.setattr(memory, "get_process_memory", lambda: {
        "rss_bytes": 10 * 1024 * 1024, "peak_rss_bytes": 30 * 1024 * 1024,
    })
    with caplog.at_level(logging.INFO, logger="kovaaks"):
        memory.log_memory("fetch_checkpoint", completed=25)
    assert "Memory [fetch_checkpoint] RSS=10.0 MiB peak=30.0 MiB completed=25" in caplog.text


@pytest.mark.parametrize("failure_stage", ["measurement", "logging"])
def test_diagnostics_never_interrupt_fetch(monkeypatch, failure_stage):
    def fail(*args, **kwargs):
        raise RuntimeError("Diagnostics unavailable")

    monkeypatch.setattr(memory, "get_process_memory", lambda: {
        "rss_bytes": None, "peak_rss_bytes": None,
    })
    if failure_stage == "measurement":
        monkeypatch.setattr(memory, "get_process_memory", fail)
    else:
        monkeypatch.setattr(memory.logger, "info", fail)
    assert memory.log_memory("fetch_checkpoint") == {"rss_bytes": None, "peak_rss_bytes": None}


def test_startup_rows_are_released_before_publishing_ready_state():
    references = []

    class WeakList(list):
        pass

    class ReadyEvent:
        calls = 0

        def set(self):
            assert references and all(reference() is None for reference in references)
            self.calls += 1

    app = SimpleNamespace(
        _load_cache_and_populate=lambda: None,
        _scores_cache={}, _cache_loaded_event=ReadyEvent(),
        _update_progress=lambda *args: None, window=None,
    )
    statuses = []
    app._update_status = statuses.append

    def rebuild():
        played, unplayed = WeakList([{}, {}]), WeakList([{}])
        references.extend([weakref.ref(played), weakref.ref(unplayed)])
        app._global_points_sum = 123
        return played, unplayed

    app._rebuild_data = rebuild
    KovaaksAPI._initial_cache_load(app)

    assert app._cache_loaded_event.calls >= 1
    assert app._global_points_sum == 123
    assert statuses == ["Rebuilt from memory cache — 2 played, 1 unplayed"]
