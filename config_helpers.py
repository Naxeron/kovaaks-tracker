"""
KovaaKs Scenario Tracker — configuration helpers.

Handles reading and writing the JSON config file, plus platform-specific
default paths.
"""

import json
import logging
import os
import sys

logger = logging.getLogger("kovaaks")

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


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
    """Load config from disk, returning empty dict if missing."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return {}


def save_config(cfg):
    """Save config to disk, stripping the password key."""
    filtered_cfg = {k: v for k, v in cfg.items() if k != "password"}
    with open(CONFIG_PATH, "w") as f:
        json.dump(filtered_cfg, f, indent=2)
