import logging
import sys
import os
from .constants import LAUNCH_MARKER
from .paths import DATA_DIR, PROJECT_DIR, existing_data_path, migrate_legacy_file

SCRIPT_DIR = PROJECT_DIR
LOG_FILE = os.path.join(DATA_DIR, "kovaaks.log")

def _trim_log_file(path, keep=2):
    """Trim the log file to only keep the last *keep* launch sessions."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return
    parts = content.split(LAUNCH_MARKER)
    if len(parts) > keep + 1:
        with open(path, "w", encoding="utf-8") as f:
            f.write(LAUNCH_MARKER.join(parts[-(keep + 1):]))

def is_debug_mode():
    """Check if the debug flag is set via command line arguments or environment variable."""
    return "--debug" in sys.argv or "-d" in sys.argv or os.environ.get("KOVAAKS_DEBUG") == "1"

def setup_logging():
    global LOG_FILE
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

    previous_log = existing_data_path(LOG_FILE)
    if previous_log != LOG_FILE:
        try:
            migrate_legacy_file(previous_log, LOG_FILE)
        except OSError as e:
            logger.warning("Could not move log to data directory: %s", e)
            LOG_FILE = previous_log
    os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
    _trim_log_file(LOG_FILE, keep=2)

    _file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    _file_handler.setLevel(log_level)
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(_file_handler)

    # Write the launch marker so future trims know where this session starts
    logger.info(LAUNCH_MARKER)
    
    return logger
