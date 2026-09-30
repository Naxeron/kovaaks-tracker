"""Round-trip and recovery coverage for the compact local history cache."""

import base64
from copy import deepcopy
import gzip
import json
from pathlib import Path
import struct
import threading

import pytest

import kovaaks.cache as cache
from kovaaks.history import CompactHistory


FIRST = "2026-09-29T12:00:00"
SECOND = "2026-09-30T12:00:00"
THIRD = "2026-09-30T13:00:00"


def write_raw_cache(data):
    """Fixtures redirect SCORES_CACHE to this test's temporary directory."""
    path = Path(cache.SCORES_CACHE)
    path.write_bytes(gzip.compress(json.dumps(data).encode("utf-8")))
    return path


def read_raw_cache():
    with gzip.open(cache.SCORES_CACHE, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def history_values(data):
    return {lid: dict(points.items()) for lid, points in data["entry_history"].items()}


def packed_history():
    return {
        "_format": "packed-history-v1",
        "timelines": [[FIRST, SECOND]],
        "series": {"one": [0, base64.b64encode(struct.pack("<2q", 123, 456)).decode("ascii")]},
    }


def test_legacy_load_compacts_and_shares_timestamps_without_rewriting_file():
    history = {
        "one": {FIRST: 100, SECOND: 150},
        "two": {FIRST: 200, SECOND: 250},
        "reversed": {SECOND: 300, FIRST: 350},
        "empty": {},
    }
    path = write_raw_cache({"entry_history": history, "scores": {"one": {"score": 42}}})
    original = path.read_bytes()

    loaded = cache.load_scores_cache()

    assert history_values(loaded) == history
    assert all(isinstance(points, CompactHistory) for points in loaded["entry_history"].values())
    assert all(points.is_compact for points in loaded["entry_history"].values())
    assert loaded["entry_history"]["one"].timestamps is loaded["entry_history"]["two"].timestamps
    assert list(loaded["entry_history"]["reversed"]) == [SECOND, FIRST]
    assert loaded["scores"] == {"one": {"score": 42}}
    assert path.read_bytes() == original


@pytest.mark.parametrize("compact_input", [False, True])
def test_packed_save_round_trip_reuses_timelines_and_preserves_integer_limits(compact_input):
    history = {
        "one": {FIRST: -(1 << 63), SECOND: (1 << 63) - 1},
        "two": {FIRST: 0, SECOND: 12345},
        "reversed": {SECOND: 7, FIRST: -8},
        "empty": {},
    }
    entries = {lid: CompactHistory(points) for lid, points in history.items()} if compact_input else history
    data = {"entry_history": entries, "scores": {"one": {"score": 42}}, "zombies": ["old"]}

    assert cache.save_scores_cache(data) is True

    encoded = read_raw_cache()
    envelope = encoded["entry_history"]
    assert envelope["_format"] == "packed-history-v1"
    assert len(envelope["timelines"]) == 3
    one, two = envelope["series"]["one"], envelope["series"]["two"]
    assert one[0] == two[0]
    assert envelope["timelines"][one[0]] == [FIRST, SECOND]
    assert struct.unpack("<2q", base64.b64decode(one[1], validate=True)) == (-(1 << 63), (1 << 63) - 1)
    assert envelope["series"]["reversed"][0] != one[0]
    assert history_values(data) == history

    loaded = cache.load_scores_cache()
    assert history_values(loaded) == history
    assert list(loaded["entry_history"]["reversed"]) == [SECOND, FIRST]
    assert loaded["entry_history"]["one"].timestamps is loaded["entry_history"]["two"].timestamps
    assert loaded["scores"] == data["scores"]
    assert loaded["zombies"] == data["zombies"]
    assert cache.save_scores_cache(loaded) is True
    assert read_raw_cache() == encoded


@pytest.mark.parametrize("unusual", [True, False, 1.25, "123", 1 << 80, -(1 << 80), None, {"samples": [1, 2]}])
def test_unusual_values_keep_exact_types_through_legacy_and_packed_fallback(unusual):
    history = {"fallback": {FIRST: unusual, SECOND: 42}, "normal": {FIRST: 100, SECOND: 200}}
    write_raw_cache({"entry_history": history})
    loaded = cache.load_scores_cache()
    assert not loaded["entry_history"]["fallback"].is_compact
    assert loaded["entry_history"]["normal"].is_compact

    assert cache.save_scores_cache(loaded) is True

    assert read_raw_cache()["entry_history"]["series"]["fallback"] == history["fallback"]
    restored = cache.load_scores_cache()
    actual = restored["entry_history"]["fallback"][FIRST]
    assert type(actual) is type(unusual)
    assert actual == unusual
    assert history_values(restored) == history


def invalid_envelopes():
    cases = []
    for value in ["packed-history-v99", None, 1]:
        envelope = packed_history()
        envelope["_format"] = value
        cases.append((f"format-{value}", envelope))
    for field in ["_format", "timelines", "series"]:
        envelope = packed_history()
        del envelope[field]
        cases.append((f"missing-{field}", envelope))
    for value in [{}, [1], [[FIRST, 1]], [[FIRST, FIRST]]]:
        envelope = packed_history()
        envelope["timelines"] = value
        cases.append((f"timeline-{value}", envelope))
    for value in [[], None]:
        envelope = packed_history()
        envelope["series"] = value
        cases.append((f"series-{value}", envelope))
    for index in [True, 0.0, "0", -1, 1]:
        envelope = packed_history()
        envelope["series"]["one"][0] = index
        cases.append((f"index-{index!r}", envelope))
    for row in [None, [], [0], [0, "", "extra"]]:
        envelope = packed_history()
        envelope["series"]["one"] = row
        cases.append((f"row-{row}", envelope))
    for value in [42, "not base64!", "AQ"]:
        envelope = packed_history()
        envelope["series"]["one"][1] = value
        cases.append((f"base64-{value}", envelope))
    for length in [0, 1, 7, 8, 15, 17, 24]:
        envelope = packed_history()
        envelope["series"]["one"][1] = base64.b64encode(b"\x00" * length).decode("ascii")
        cases.append((f"count-bytes-{length}", envelope))
    return cases


@pytest.mark.parametrize("label,envelope", invalid_envelopes(), ids=lambda value: value if isinstance(value, str) else None)
def test_invalid_packed_cache_is_rejected_without_changing_original(label, envelope, caplog):
    path = write_raw_cache({"entry_history": envelope, "scores": {"one": {"score": 42}}})
    original = path.read_bytes()

    assert cache.load_scores_cache() == {}, label

    assert path.read_bytes() == original
    assert "Could not load cache" in caplog.text
    assert not list(path.parent.glob("*.tmp"))


def test_deepcopy_snapshot_stays_isolated_before_and_during_json_save(monkeypatch):
    live = {"entry_history": {
        "one": CompactHistory({FIRST: 10, SECOND: 20}),
        "fallback": CompactHistory({FIRST: {"samples": [1, 2]}, SECOND: True}),
    }}
    snapshot = deepcopy(live)
    live["entry_history"]["one"][FIRST] = 999
    live["entry_history"]["fallback"][FIRST]["samples"].append(3)
    snapshot["entry_history"]["one"][THIRD] = 30
    expected = history_values(snapshot)
    assert THIRD not in live["entry_history"]["one"]
    assert snapshot["entry_history"]["fallback"][FIRST]["samples"] == [1, 2]
    json_dump = json.dump
    mutations = []

    def mutate_live_during_dump(encoded, stream, **kwargs):
        del live["entry_history"]["one"][SECOND]
        live["entry_history"]["one"][THIRD] = 777
        live["entry_history"]["fallback"][FIRST]["samples"].append(4)
        mutations.append(True)
        return json_dump(encoded, stream, **kwargs)

    monkeypatch.setattr(cache.json, "dump", mutate_live_during_dump)

    assert cache.save_scores_cache(snapshot) is True

    assert mutations == [True]
    assert history_values(snapshot) == expected
    assert history_values(cache.load_scores_cache()) == expected
    assert live["entry_history"]["one"][THIRD] == 777
    assert live["entry_history"]["fallback"][FIRST]["samples"] == [1, 2, 3, 4]


def test_writer_saves_detached_compact_snapshots_during_live_mutation():
    live = {"entry_history": {"one": CompactHistory({FIRST: 10, SECOND: 20})}}
    lock = threading.RLock()
    save_started = threading.Event()
    release_save = threading.Event()
    versions = []

    def snapshot():
        with lock:
            return deepcopy(live)

    def save(detached):
        if not versions:
            save_started.set()
            assert release_save.wait(2), "Test did not release the first save"
        assert cache.save_scores_cache(detached)
        versions.append(history_values(cache.load_scores_cache()))
        return True

    writer = cache.CacheWriter(snapshot, save=save)
    assert writer.request()
    try:
        assert save_started.wait(2), "Cache writer did not take its first snapshot"
        with lock:
            live["entry_history"]["one"][FIRST] = 100
            live["entry_history"]["one"][THIRD] = 300
        assert writer.request()
    finally:
        release_save.set()
        assert writer.flush()

    assert versions == [
        {"one": {FIRST: 10, SECOND: 20}},
        {"one": {FIRST: 100, SECOND: 20, THIRD: 300}},
    ]
