"""
Tests for cache loading utilities.
"""
import sys
import os
import gzip

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovaaks.cache import load_scenarios_from_cache
import kovaaks.cache as cache_helpers


class TestLoadScoresCache:
    @pytest.mark.parametrize("contents", [
        b"\xff", b"[]", b"null", b'"cache"', b'{"scenarios":',
    ])
    def test_invalid_cache_returns_empty_and_preserves_file(self, tmp_path, monkeypatch, contents, caplog):
        """Decode/type errors should activate recovery without changing cache bytes."""
        path = tmp_path / "cache.json.gz"
        monkeypatch.setattr(cache_helpers, "SCORES_CACHE", str(path))
        path.write_bytes(gzip.compress(contents))
        previous = path.read_bytes()

        assert cache_helpers.load_scores_cache() == {}
        assert path.read_bytes() == previous
        assert "Could not load cache" in caplog.text

    def test_invalid_compression_returns_empty_and_preserves_file(self, tmp_path, monkeypatch, caplog):
        path = tmp_path / "cache.json.gz"
        monkeypatch.setattr(cache_helpers, "SCORES_CACHE", str(path))
        # A valid gzip header followed by a reserved DEFLATE block type.
        contents = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff\xff" + b"\x00" * 8
        path.write_bytes(contents)

        assert cache_helpers.load_scores_cache() == {}
        assert path.read_bytes() == contents
        assert "Could not load cache" in caplog.text


class TestLoadScenariosFromCache:
    def test_loads_from_valid_cache(self, sample_cache):
        result = load_scenarios_from_cache(sample_cache)
        assert len(result) == 5
        assert result[0]["scenarioName"] == "1w6ts Reload"

    def test_empty_cache_returns_empty(self):
        result = load_scenarios_from_cache({})
        assert result == []

    def test_missing_scenarios_key(self):
        result = load_scenarios_from_cache({"scores": {}})
        assert result == []

    def test_scenarios_key_empty_list(self):
        result = load_scenarios_from_cache({"scenarios": []})
        assert result == []

    def test_preserves_all_scenario_fields(self, sample_cache):
        result = load_scenarios_from_cache(sample_cache)
        first = result[0]
        assert "leaderboardId" in first
        assert "scenarioName" in first
        assert "counts" in first
        assert "entries" in first["counts"]
