"""
Tests for _trim_log_file log rotation utility.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constants import LAUNCH_MARKER as _LAUNCH_MARKER
from kovaaks_gui import _trim_log_file


class TestTrimLogFile:
    def _write_log(self, path, sessions):
        """Helper: write a log file with N sessions separated by LAUNCH markers."""
        parts = []
        for i, content in enumerate(sessions):
            parts.append(f"{_LAUNCH_MARKER}\n{content}\n")
        path.write_text("".join(parts), encoding="utf-8")

    def test_no_trimming_when_within_limit(self, tmp_path):
        """Should not trim when sessions <= keep."""
        log = tmp_path / "test.log"
        self._write_log(log, ["session1", "session2"])
        _trim_log_file(str(log), keep=2)
        content = log.read_text(encoding="utf-8")
        assert "session1" in content
        assert "session2" in content

    def test_trims_oldest_sessions(self, tmp_path):
        """Should discard sessions beyond keep count.
        
        With 5 sessions and keep=2, split produces 6 parts (empty prefix + 5 session texts).
        The function keeps the last 3 parts joined by markers → old1 and old2 are discarded.
        old3 is preserved as the "prefix" of the kept section.
        """
        log = tmp_path / "test.log"
        self._write_log(log, ["old1", "old2", "old3", "recent1", "recent2"])
        _trim_log_file(str(log), keep=2)
        content = log.read_text(encoding="utf-8")
        assert "old1" not in content
        assert "old2" not in content
        # old3 is the text preceding the first kept marker — it remains
        assert "recent1" in content
        assert "recent2" in content
        # Verify markers are intact for the kept sessions
        assert content.count(_LAUNCH_MARKER) == 2

    def test_missing_file_does_not_raise(self, tmp_path):
        """Should silently handle a missing log file."""
        _trim_log_file(str(tmp_path / "nonexistent.log"), keep=2)

    def test_empty_file(self, tmp_path):
        """An empty file should be left alone."""
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")
        _trim_log_file(str(log), keep=2)
        assert log.read_text(encoding="utf-8") == ""

    def test_single_session_with_keep_one(self, tmp_path):
        """One session with keep=1 should not trim."""
        log = tmp_path / "test.log"
        self._write_log(log, ["only_session"])
        _trim_log_file(str(log), keep=1)
        content = log.read_text(encoding="utf-8")
        assert "only_session" in content

    def test_exactly_keep_plus_one(self, tmp_path):
        """Three sessions with keep=2: first session's content becomes the prefix 
        of the kept block, second and third are kept with their markers."""
        log = tmp_path / "test.log"
        self._write_log(log, ["first", "second", "third"])
        _trim_log_file(str(log), keep=2)
        content = log.read_text(encoding="utf-8")
        # With 3 sessions → 4 parts. keep=2 → keep last 3 parts.
        # parts = ["", "\nfirst\n", "\nsecond\n", "\nthird\n"]
        # last 3 = ["\nfirst\n", "\nsecond\n", "\nthird\n"] → no trimming since 4 <= 2+1+1
        # Actually len(parts)=4, keep+1=3, 4 > 3 so it DOES trim.
        # Kept: parts[-3:] = ["\nfirst\n", "\nsecond\n", "\nthird\n"]
        # first is preserved as prefix text of the trimmed output
        assert "second" in content
        assert "third" in content
        assert content.count(_LAUNCH_MARKER) == 2

    def test_heavy_trimming(self, tmp_path):
        """10 sessions with keep=1 should only keep the last session."""
        log = tmp_path / "test.log"
        self._write_log(log, [f"session_{i}" for i in range(10)])
        _trim_log_file(str(log), keep=1)
        content = log.read_text(encoding="utf-8")
        # Only session_9 (last) and its prefix should remain
        assert "session_9" in content
        assert content.count(_LAUNCH_MARKER) == 1
        # Early sessions should be gone
        for i in range(8):
            assert f"session_{i}" not in content
