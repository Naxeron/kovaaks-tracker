"""Compact histories preserve mapping behavior and detached cache snapshots."""

from collections.abc import MutableMapping
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
import datetime
import gc
import struct
import weakref

import pytest

from kovaaks.history import CompactHistory
from kovaaks.scoring import parse_popularity_metrics, prune_entry_history


def test_mapping_reads_preserve_order_values_and_views():
    original = {"2026-09-28": 50, "2026-09-29": 70, "2026-09-30": 100}
    history = CompactHistory(original)

    assert isinstance(history, MutableMapping)
    assert history == original
    assert len(history) == 3
    assert list(history) == list(original)
    assert history.keys() == original.keys()
    assert history.items() == original.items()
    assert len(history.items()) == 3
    assert list(history.values()) == [50, 70, 100]
    assert history.get("missing", 42) == 42
    assert "missing" not in history
    with pytest.raises(KeyError):
        history["missing"]


def test_mutations_preserve_mapping_semantics():
    history = CompactHistory({"old": 10, "new": 20})
    history["old"] = 11
    history["latest"] = 30
    assert list(history.items()) == [("old", 11), ("new", 20), ("latest", 30)]
    assert history.pop("new") == 20
    assert history.setdefault("old", 100) == 11
    assert history.setdefault("other", 40) == 40
    history.update({"old": 12, "final": 50})
    assert history == {"old": 12, "latest": 30, "other": 40, "final": 50}
    with pytest.raises(KeyError):
        del history["missing"]
    history.clear()
    assert not history
    assert history.is_compact
    assert history.packed_counts == b""


def test_timestamps_are_shared_but_count_buffers_are_independent():
    first = CompactHistory({"one": 10, "two": 20})
    second = CompactHistory({"one": 100, "two": 200})

    assert first.timestamps is second.timestamps
    assert first._axis is second._axis
    assert struct.unpack("<2q", first.packed_counts) == (10, 20)
    assert struct.unpack("<2q", second.packed_counts) == (100, 200)


@pytest.mark.parametrize("clone", [copy, deepcopy, lambda history: history.copy()])
def test_snapshot_shares_immutable_buffers_and_detaches_mutations(clone):
    source = CompactHistory({"one": 10, "two": 20})
    snapshot = clone(source)

    assert snapshot.timestamps is source.timestamps
    assert snapshot.packed_counts is source.packed_counts
    source["one"] = 11
    source["three"] = 30
    del source["two"]
    assert source == {"one": 11, "three": 30}
    assert snapshot == {"one": 10, "two": 20}

    snapshot["two"] = 21
    assert source == {"one": 11, "three": 30}
    assert snapshot == {"one": 10, "two": 21}


def test_unchanged_assignment_preserves_buffer():
    history = CompactHistory({"one": 10})
    packed = history.packed_counts
    history["one"] = 10
    assert history.packed_counts is packed


@pytest.mark.parametrize("value", [True, False, 1.5, "bad", None, 1 << 63, -(1 << 63) - 1])
def test_unsupported_values_fall_back_without_coercion(value):
    history = CompactHistory({"one": value, "two": 2})

    assert not history.is_compact
    assert history.packed_counts is None
    assert history["one"] is value
    assert history == {"one": value, "two": 2}


def test_supported_integer_boundaries_round_trip():
    values = [-(1 << 63), 0, (1 << 63) - 1]
    history = CompactHistory.from_counts(["one", "two", "three"], values)
    assert history.is_compact
    assert list(history.values()) == values


def test_non_string_keys_are_preserved_in_fallback():
    history = CompactHistory({1: 2, None: 3, "one": 4})
    assert not history.is_compact
    assert history == {1: 2, None: 3, "one": 4}


def test_unsupported_assignment_detaches_existing_compact_snapshot():
    history = CompactHistory({"one": 10})
    snapshot = deepcopy(history)
    history["one"] = {"nested": [20]}
    assert not history.is_compact
    assert snapshot.is_compact
    assert snapshot == {"one": 10}
    second_snapshot = deepcopy(history)
    history["one"]["nested"].append(30)
    assert second_snapshot["one"] == {"nested": [20]}


def test_fallback_deepcopy_preserves_cycles_without_sharing_mutable_data():
    history = CompactHistory({"one": 1})
    history["self"] = history
    snapshot = deepcopy(history)
    assert snapshot is not history
    assert snapshot["self"] is snapshot
    snapshot["one"] = 2
    assert history["one"] == 1


def test_unhashable_assignment_does_not_damage_existing_data():
    history = CompactHistory({"one": 1})
    with pytest.raises(TypeError):
        history[[]] = 2
    assert history.is_compact
    assert history == {"one": 1}


def test_parallel_arrays_validate_lengths_and_handle_duplicate_keys_like_dict():
    with pytest.raises(ValueError, match="equal lengths"):
        CompactHistory.from_counts(["one", "two"], [1])
    history = CompactHistory.from_counts(["one", "one"], [1, 2])
    assert history == {"one": 2}


def test_packed_constructor_reuses_immutable_bytes_without_decoding_counts():
    packed = struct.pack("<3q", -(1 << 63), 0, (1 << 63) - 1)
    history = CompactHistory.from_packed(["one", "two", "three"], packed)

    assert history.packed_counts is packed
    assert history == {"one": -(1 << 63), "two": 0, "three": (1 << 63) - 1}
    mutable = bytearray(packed)
    other = CompactHistory.from_packed(history.timestamps, mutable)
    mutable[:] = bytes(len(mutable))
    assert other == history
    assert other.timestamps is history.timestamps


@pytest.mark.parametrize("timestamps, packed", [
    (["one"], b""), (["one"], bytes(9)),
    (["one", "one"], bytes(16)), ([1], bytes(8)),
    (["one"], "12345678"), ([], 0),
])
def test_packed_constructor_rejects_invalid_storage(timestamps, packed):
    with pytest.raises(ValueError):
        CompactHistory.from_packed(timestamps, packed)


def test_prepared_timestamps_reuse_axis_and_still_validate_each_count_buffer():
    axis = CompactHistory.prepare_timestamps(["one", "two"])
    first = CompactHistory.from_packed(axis, struct.pack("<2q", 1, 2))
    second = CompactHistory.from_packed(axis, struct.pack("<2q", 3, 4))

    assert first._axis is second._axis is axis
    assert first == {"one": 1, "two": 2}
    assert second == {"one": 3, "two": 4}
    with pytest.raises(ValueError, match="one int64 per timestamp"):
        CompactHistory.from_packed(axis, bytes(8))
    with pytest.raises(ValueError, match="must be bytes"):
        CompactHistory.from_packed(axis, "0" * 16)


@pytest.mark.parametrize("compact", [True, False])
def test_retain_timestamps_preserves_values_order_and_snapshot(compact):
    original = {"one": 1, "two": 2, "three": 3 if compact else 3.5}
    history = CompactHistory(original)
    snapshot = deepcopy(history)

    history.retain_timestamps(["three", "one"])

    assert list(history.items()) == [("three", original["three"]), ("one", 1)]
    assert snapshot == original
    before = dict(history.items())
    with pytest.raises(KeyError):
        history.retain_timestamps(["one", "missing"])
    assert history == before
    history.retain_timestamps([])
    assert not history
    assert snapshot == original


def test_bulk_update_preserves_order_and_snapshot_without_repacking_unchanged_values():
    history = CompactHistory({"one": 1, "two": 2})
    snapshot = deepcopy(history)
    packed = history.packed_counts
    history.update({"one": 1})
    assert history.packed_counts is packed
    history.update({"one": 10, "three": 3}, four=4)
    assert list(history.items()) == [("one", 10), ("two", 2), ("three", 3), ("four", 4)]
    assert snapshot == {"one": 1, "two": 2}
    history.update({"two": 2.0})
    assert not history.is_compact
    assert type(history["two"]) is float


def test_concurrent_constructors_share_one_axis():
    with ThreadPoolExecutor(max_workers=4) as executor:
        histories = list(executor.map(
            lambda value: CompactHistory({"shared-one": value, "shared-two": value + 1}),
            range(32),
        ))
    assert all(history._axis is histories[0]._axis for history in histories)


def test_unused_timestamp_axes_can_be_collected():
    history = CompactHistory({"unique-axis-for-collection": 1})
    reference = weakref.ref(history._axis)
    del history
    gc.collect()
    assert reference() is None


def test_existing_history_pruning_and_trend_helpers_accept_compact_mapping():
    now = datetime.datetime(2026, 9, 30, 12)
    original = {(now - datetime.timedelta(hours=hour)).isoformat(): 1000 - hour
                for hour in range(200)}
    compact = CompactHistory(original)
    reference = dict(original)

    assert prune_entry_history({"one": compact}, now)
    assert prune_entry_history({"one": reference}, now)

    assert compact == reference
    assert len(compact) == 168
    assert parse_popularity_metrics(compact) == parse_popularity_metrics(reference)


@pytest.mark.parametrize("limit", [0, 1, 3, 168, -1])
def test_shared_pruning_matches_dicts_with_invalid_future_and_unsorted_dates(limit):
    now = datetime.datetime(2026, 9, 30, 12)
    stamps = ["2026-09-30T11:00:00", "invalid", "2026-09-27", "2026-09-30T14:00:00",
              "2026-09-30T09:00:00", "2026-09-30T10:00:00"]
    originals = {str(index): dict(zip(stamps, range(index, index + len(stamps))))
                 for index in range(3)}
    originals["fallback"] = {**originals["0"], "2026-09-30T08:00:00": 1.5}
    compact = {lid: CompactHistory(points) for lid, points in originals.items()}
    snapshots = deepcopy(compact)
    reference = deepcopy(originals)

    assert prune_entry_history(compact, now, limit) == prune_entry_history(reference, now, limit)

    assert compact == reference
    assert snapshots == originals
    assert all(list(compact[lid]) == list(points) for lid, points in reference.items())
    assert compact["0"].timestamps is compact["1"].timestamps


def test_shared_pruning_does_not_scan_each_unchanged_scenario(monkeypatch):
    now = datetime.datetime(2026, 9, 30, 12)
    stamps = [(now - datetime.timedelta(hours=hour)).isoformat() for hour in range(168)]
    history = {str(index): CompactHistory(dict.fromkeys(stamps, index)) for index in range(100)}
    axes = {id(points.timestamps) for points in history.values()}
    buffers = {lid: points.packed_counts for lid, points in history.items()}
    original_iter = CompactHistory.__iter__
    scanned = []

    def scan(points):
        scanned.append(points)
        return original_iter(points)

    monkeypatch.setattr(CompactHistory, "__iter__", scan)

    assert prune_entry_history(history, now) is False

    assert len(scanned) <= len(axes)
    assert all(points.packed_counts is buffers[lid] for lid, points in history.items())


@pytest.mark.parametrize("limit", [0, 1, 3, -1])
@pytest.mark.parametrize("original", [{}, {"2026-09-30T11:00:00": 1}])
def test_shared_pruning_retains_existing_limit_and_changed_semantics(limit, original):
    now = datetime.datetime(2026, 9, 30, 12)
    compact = {"one": CompactHistory(original)}
    reference = {"one": dict(original)}

    assert prune_entry_history(compact, now, limit) == prune_entry_history(reference, now, limit)
    assert compact == reference


def test_popularity_batch_reuses_shared_dates_but_reads_each_scenarios_counts(monkeypatch):
    import kovaaks.scoring as scoring

    stamps = ["2026-09-30T12:00:00", "2026-09-28T12:00:00", "2026-09-29T12:00:00"]
    first = CompactHistory(dict(zip(stamps, [140, 100, 110])))
    second = CompactHistory(dict(zip(stamps, [280, 200, 220])))
    parsed = []
    parse = scoring.parse_iso_dt

    def parse_once(stamp):
        parsed.append(stamp)
        return parse(stamp)

    monkeypatch.setattr(scoring, "parse_iso_dt", parse_once)
    batch = {}

    assert parse_popularity_metrics(first, timeline_cache=batch) == (20.0, 30)
    first_parse_count = len(parsed)
    assert parse_popularity_metrics(second, timeline_cache=batch) == (40.0, 60)
    assert len(parsed) == first_parse_count
    assert len(batch) == 1
    second[stamps[0]] = 300
    assert parse_popularity_metrics(second, timeline_cache=batch) == (50.0, 80)
    assert len(parsed) == first_parse_count


@pytest.mark.parametrize("points,expected", [
    ({"2026-09-28": 100, "invalid": 200}, (0.0, 0)),
    ({"2026-09-28": 100, "2026-09-29-invalid": 150, "2026-09-30": 200}, (50.0, 0)),
    ({"2026-09-30T12:00:00": 100, "2026-09-30T12:20:00": 200}, (0.0, 0)),
    ({"2026-09-28": "bad", "2026-09-30": 200}, (0.0, 0)),
    ({"2026-09-28": 100.5, "2026-09-30": 200.5}, (50.0, 100.0)),
])
def test_popularity_batch_preserves_malformed_partial_results_and_fallbacks(points, expected):
    compact = CompactHistory(points)
    batch = {}

    assert parse_popularity_metrics(points) == expected
    assert parse_popularity_metrics(compact, timeline_cache=batch) == expected
    assert parse_popularity_metrics(compact, timeline_cache=batch) == expected


def test_popularity_batch_handles_axis_mutation_and_new_batches():
    history = CompactHistory({"2026-09-28": 100, "2026-09-29": 110, "2026-09-30": 140})
    old_axis = history.timestamps
    batch = {}
    assert parse_popularity_metrics(history, timeline_cache=batch) == (20.0, 30)

    history.retain_timestamps(["2026-09-29", "2026-09-30"])
    history["2026-10-01"] = 200

    assert parse_popularity_metrics(history, timeline_cache=batch) == (45.0, 60)
    assert batch[id(old_axis)][0] is old_axis
    assert len(batch) == 2
    next_batch = {}
    assert parse_popularity_metrics(history, timeline_cache=next_batch) == (45.0, 60)
    assert len(next_batch) == 1
