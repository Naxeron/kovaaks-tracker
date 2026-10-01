"""
KovaaKs Scenario Tracker — configuration helpers.

Handles reading and writing the JSON config file, plus platform-specific
default paths.
"""

import json
import logging
import os
import sys
import tempfile
import threading

from .paths import DATA_DIR, PROJECT_DIR, existing_data_path, migrate_legacy_file

logger = logging.getLogger("kovaaks")

SCRIPT_DIR = PROJECT_DIR
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

_config_lock = threading.Lock()


def get_default_stats_dir():
    """Return the platform-specific default KovaaK's stats directory."""
    if sys.platform == "win32":
        return (r"C:\Program Files (x86)\Steam\steamapps\common"
                r"\FPSAimTrainer\FPSAimTrainer\stats")
    else:
        return os.path.expanduser(
            "~/.local/share/Steam/steamapps/common"
            "/FPSAimTrainer/FPSAimTrainer/stats/")


def _read_config(path):
    """Return a valid config, or None without modifying an unreadable file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.warning("Could not load config: %s", e)
        return None

    if not isinstance(cfg, dict):
        logger.warning("Could not load config: expected a JSON object")
        return None
    return cfg


def load_config():
    """Load settings, moving a valid legacy config into data/ on first use."""
    global CONFIG_PATH
    with _config_lock:
        path = existing_data_path(CONFIG_PATH)
        cfg = _read_config(path)
        if cfg is None:
            if path != CONFIG_PATH and not os.path.lexists(path):
                # A second process may have moved the legacy file between path
                # selection and opening it. Read its published destination.
                return _read_config(CONFIG_PATH) or {}
            return {}
        if path != CONFIG_PATH:
            try:
                migrate_legacy_file(path, CONFIG_PATH)
            except OSError as e:
                logger.warning("Could not move config to data directory: %s", e)
                # Password cleanup and future saves must still update the file
                # we loaded. A fresh launch will retry the preferred data path.
                CONFIG_PATH = path
            else:
                # An existing destination always wins, including when another
                # process created it after we read the legacy config.
                cfg = _read_config(CONFIG_PATH)
        return cfg or {}


def save_config(cfg):
    """Save config atomically, stripping the password key.

    Failed writes leave the previous config intact and propagate the error to
    the caller. Unique temporary files also keep concurrent processes isolated.
    """
    filtered_cfg = {k: v for k, v in cfg.items() if k != "password"}
    with _config_lock:
        tmp_config = None
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", delete=False,
                dir=os.path.dirname(CONFIG_PATH) or ".",
                prefix=f".{os.path.basename(CONFIG_PATH)}.{os.getpid()}.",
                suffix=".tmp",
            ) as f:
                tmp_config = f.name
                json.dump(filtered_cfg, f, indent=2)
            os.replace(tmp_config, CONFIG_PATH)
            tmp_config = None
        finally:
            if tmp_config is not None:
                try:
                    os.remove(tmp_config)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    logger.warning("Could not remove temporary config: %s", e)
