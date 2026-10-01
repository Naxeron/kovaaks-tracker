"""Best-effort process memory diagnostics without optional dependencies."""

import logging
from functools import lru_cache
import os
import subprocess
import sys


logger = logging.getLogger("kovaaks")


@lru_cache(maxsize=1)
def _get_malloc_trim():
    """Resolve glibc's optional allocator cleanup once, retaining its library."""
    try:
        import ctypes

        trim = ctypes.CDLL(None).malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return trim
    except Exception:
        return None


def release_unused_memory():
    """Ask Linux's allocator to return free pages after large temporary work.

    This does not collect live Python objects or change allocator settings. It
    is best effort: unsupported allocators/platforms and native-call failures
    are harmless, and the return value only indicates whether pages were freed.
    Call at infrequent background-work boundaries, after dropping temporaries.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        trim = _get_malloc_trim()
        return bool(trim(0)) if trim is not None else False
    except Exception:
        return False


def _read_linux_memory(path="/proc/self/status"):
    """Read current and high-water resident memory, both reported in KiB."""
    fields = {"VmRSS": "rss_bytes", "VmHWM": "peak_rss_bytes"}
    result = {}
    with open(path, encoding="ascii", errors="replace") as stream:
        for line in stream:
            name, _, raw = line.partition(":")
            parts = raw.split()
            if name not in fields or len(parts) != 2 or parts[1] != "kB":
                continue
            try:
                value = int(parts[0])
            except ValueError:
                continue
            if value >= 0:
                result[fields[name]] = value * 1024
    return result


def _read_windows_memory():
    """Use the process working set and its peak through the native Windows API."""
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    process_status = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    process_status.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD,
    ]
    process_status.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not process_status.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise OSError("Process memory counters are unavailable")
    return {"rss_bytes": counters.WorkingSetSize, "peak_rss_bytes": counters.PeakWorkingSetSize}


def _read_ps_memory():
    """Use a bounded, shell-free ps call where native current-RSS data is absent."""
    result = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())],
        capture_output=True, text=True, check=True, timeout=0.5,
    )
    value = int(result.stdout.strip())
    return value * 1024 if value >= 0 else None


def _read_peak_memory():
    """Normalize resource's peak RSS: bytes on macOS, KiB on other Unix hosts."""
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value * (1 if sys.platform == "darwin" else 1024)) if value >= 0 else None


def get_process_memory():
    """Return current/peak resident bytes, with None for unavailable metrics.

    Measurements concern this Python process rather than the web renderer or
    other child processes. Diagnostic failures never interrupt application work.
    """
    result = {"rss_bytes": None, "peak_rss_bytes": None}
    try:
        if sys.platform.startswith("linux"):
            result.update(_read_linux_memory())
        elif sys.platform == "win32":
            result.update(_read_windows_memory())
    except Exception:
        pass
    if result["rss_bytes"] is None and sys.platform != "win32":
        try:
            result["rss_bytes"] = _read_ps_memory()
        except Exception:
            pass
    if result["peak_rss_bytes"] is None:
        try:
            result["peak_rss_bytes"] = _read_peak_memory()
        except Exception:
            pass
    return result


def log_memory(stage, **counts):
    """Log one lifecycle measurement, preserving fetch behavior on any failure."""
    try:
        measurement = get_process_memory()

        def display(value):
            return "unavailable" if value is None else f"{value / (1024 * 1024):.1f} MiB"

        details = "".join(f" {name}={value}" for name, value in counts.items())
        logger.info("Memory [%s] RSS=%s peak=%s%s", stage,
                    display(measurement["rss_bytes"]), display(measurement["peak_rss_bytes"]), details)
        return measurement
    except Exception:
        return {"rss_bytes": None, "peak_rss_bytes": None}
