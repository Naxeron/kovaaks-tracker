import logging
import sys
import os
from constants import LAUNCH_MARKER

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "kovaaks.log")

def _trim_log_file(path, keep=2):
    """Trim the log file to only keep the last *keep* launch sessions."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return
    marker = LAUNCH_MARKER
    parts = content.split(marker)
    # parts[0] is text before the first marker (possibly empty),
    # parts[1..] each start right after a marker.
    if len(parts) <= keep + 1:
        return  # nothing to trim
    # Keep only the last `keep` sections (plus the text after their markers)
    trimmed = marker.join(parts[-(keep + 1):])
    with open(path, "w", encoding="utf-8") as f:
        f.write(trimmed)

def is_debug_mode():
    """Check if the debug flag is set via command line arguments or environment variable."""
    return "--debug" in sys.argv or "-d" in sys.argv or os.environ.get("KOVAAKS_DEBUG") == "1"

def setup_logging():
    logger = logging.getLogger("kovaaks")
    
    # Avoid adding handlers multiple times
    if logger.handlers:
        return logger

    log_level = logging.DEBUG if is_debug_mode() else logging.INFO
    logger.setLevel(log_level)

    _console_handler = logging.StreamHandler(sys.stderr)
    _console_handler.setLevel(log_level)
    _console_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_console_handler)

    _trim_log_file(LOG_FILE, keep=2)

    _file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    _file_handler.setLevel(log_level)
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(_file_handler)

    # Write the launch marker so future trims know where this session starts
    logger.info(LAUNCH_MARKER)
    
    return logger

class StdoutRedirector:
    """Redirect stdout/stderr writes to a callback while keeping the original."""

    def __init__(self, callback, original):
        self._callback = callback
        self._original = original

    def write(self, text):
        if self._original:
            self._original.write(text)
        if text and text.strip():
            self._callback(text.strip())
        return len(text) if text else 0

    def flush(self):
        if self._original:
            self._original.flush()
