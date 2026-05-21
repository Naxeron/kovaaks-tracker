"""
Tests for logging helper functions and debug mode options.
"""
import sys
import os
import logging
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging_helpers

class TestLoggingDebugMode:
    def test_default_is_not_debug(self):
        # Ensure we patch out command line arguments and environment variables
        with patch.object(sys, "argv", ["kovaaks_gui.py"]), \
             patch.dict(os.environ, {}, clear=True):
            assert not logging_helpers.is_debug_mode()

    def test_debug_with_long_flag(self):
        with patch.object(sys, "argv", ["kovaaks_gui.py", "--debug"]), \
             patch.dict(os.environ, {}, clear=True):
            assert logging_helpers.is_debug_mode()

    def test_debug_with_short_flag(self):
        with patch.object(sys, "argv", ["kovaaks_gui.py", "-d"]), \
             patch.dict(os.environ, {}, clear=True):
            assert logging_helpers.is_debug_mode()

    def test_debug_with_env_var(self):
        with patch.object(sys, "argv", ["kovaaks_gui.py"]), \
             patch.dict(os.environ, {"KOVAAKS_DEBUG": "1"}):
            assert logging_helpers.is_debug_mode()

    def test_setup_logging_level_default(self, tmp_path):
        log_file = tmp_path / "test.log"
        # Patch LOG_FILE and is_debug_mode inside logging_helpers
        with patch.object(sys, "argv", ["kovaaks_gui.py"]), \
             patch.dict(os.environ, {}, clear=True), \
             patch("logging_helpers.LOG_FILE", str(log_file)), \
             patch("logging.getLogger") as mock_get_logger:
            
            mock_logger = mock_get_logger.return_value
            mock_logger.handlers = []
            
            # Call setup_logging
            logging_helpers.setup_logging()
            
            # Check if setLevel was called with INFO
            mock_logger.setLevel.assert_called_with(logging.INFO)

    def test_setup_logging_level_debug(self, tmp_path):
        log_file = tmp_path / "test.log"
        # Patch LOG_FILE and is_debug_mode inside logging_helpers to simulate debug mode
        with patch.object(sys, "argv", ["kovaaks_gui.py", "--debug"]), \
             patch.dict(os.environ, {}, clear=True), \
             patch("logging_helpers.LOG_FILE", str(log_file)), \
             patch("logging.getLogger") as mock_get_logger:
            
            mock_logger = mock_get_logger.return_value
            mock_logger.handlers = []
            
            # Call setup_logging
            logging_helpers.setup_logging()
            
            # Check if setLevel was called with DEBUG
            mock_logger.setLevel.assert_called_with(logging.DEBUG)
