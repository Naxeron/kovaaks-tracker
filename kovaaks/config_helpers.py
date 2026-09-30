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

logger = logging.getLogger("kovaaks")

SCRIPT_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

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


def load_config():
    """Load config from disk, returning empty dict if missing or unreadable."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.warning("Could not load config: %s", e)
        return {}

    if not isinstance(cfg, dict):
        logger.warning("Could not load config: expected a JSON object")
        return {}
    return cfg


def save_config(cfg):
    """Save config atomically, stripping the password key.

    Failed writes leave the previous config intact and propagate the error to
    the caller. Unique temporary files also keep concurrent processes isolated.
    """
    filtered_cfg = {k: v for k, v in cfg.items() if k != "password"}
    with _config_lock:
        tmp_config = None
        try:
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
