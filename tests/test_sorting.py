"""
Tests for natural_sort_key sorting utility.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovaaks.data_processing import natural_sort_key


class TestNaturalSortKey:
    def test_integer_string(self):
        key = natural_sort_key("42")
        assert key == (0, 42.0)

    def test_float_string(self):
        key = natural_sort_key("3.14")
        assert key == (0, 3.14)

    def test_negative_number(self):
        key = natural_sort_key("-10")
        assert key == (0, -10.0)

    def test_comma_separated_number(self):
        """Numbers with commas (e.g. '1,234') should parse correctly."""
        key = natural_sort_key("1,234")
        assert key == (0, 1234.0)

    def test_percentage_string(self):
        """Percentages like '95.50%' should be treated as numbers."""
        key = natural_sort_key("95.50%")
        assert key == (0, 95.50)

    def test_negative_percentage(self):
        key = natural_sort_key("-47.20%")
        assert key == (0, -47.20)

    def test_plain_string(self):
        """Non-numeric strings should sort after numbers."""
        key = natural_sort_key("hello")
        assert key[0] == 1
        assert key[1] == "hello"

    def test_empty_string(self):
        """Empty strings should sort last."""
        key = natural_sort_key("")
        assert key == (2, "")

    def test_whitespace_only(self):
        key = natural_sort_key("   ")
        assert key == (2, "")

    def test_sort_order_numbers_before_strings(self):
        """Numeric values should sort before strings."""
        items = ["banana", "42", "apple", "100"]
        sorted_items = sorted(items, key=natural_sort_key)
        assert sorted_items == ["42", "100", "apple", "banana"]

    def test_sort_order_empty_last(self):
        """Empty strings should sort after everything else."""
        items = ["", "10", "abc", ""]
        sorted_items = sorted(items, key=natural_sort_key)
        assert sorted_items == ["10", "abc", "", ""]

    def test_case_insensitive_string_sort(self):
        """String comparison should be case-insensitive."""
        key_lower = natural_sort_key("apple")
        key_upper = natural_sort_key("Apple")
        assert key_lower == key_upper
