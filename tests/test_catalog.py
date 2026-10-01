"""Shared scenario snapshots preserve metadata while isolating mutable data."""

from copy import copy, deepcopy
import gzip
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from kovaaks import cache, fetch_worker
from kovaaks.catalog import FrozenDict, FrozenList, freeze_catalog, freeze_json
from kovaaks.history import CompactHistory
from kovaaks_web import KovaaksAPI


@pytest.fixture
def scenario_data():
    return [{
        "leaderboardId": "42", "scenarioName": "Example", "counts": {"entries": 250},
        "scenario": {"aimType": "tracking", "authors": [{"name": "Author"}],
                     "description": "All original metadata stays available"},
        "extra": [None, True, 2.5, {"unknownField": [1, 2]}],
    }]


def test_catalog_keeps_json_fields_equality_and_iteration(scenario_data):
    catalog = freeze_catalog(scenario_data)

    assert isinstance(catalog, list)
    assert isinstance(catalog[0], dict)
    assert catalog == scenario_data
    assert list(catalog) == scenario_data
    assert json.loads(json.dumps(catalog)) == scenario_data
    assert json.loads("".join(json.JSONEncoder().iterencode(catalog))) == scenario_data
    assert freeze_catalog(catalog) is catalog
    assert copy(catalog) is deepcopy(catalog) is catalog
    assert copy(catalog[0]) is deepcopy(catalog[0]) is catalog[0]


def test_freezing_detaches_all_source_containers(scenario_data):
    expected = deepcopy(scenario_data)
    catalog = freeze_catalog(scenario_data)
    scenario_data[0]["counts"]["entries"] = 999
    scenario_data[0]["scenario"]["authors"][0]["name"] = "Changed"
    scenario_data[0]["extra"][3]["unknownField"].append(3)
    scenario_data.clear()

    assert catalog == expected


@pytest.mark.parametrize("mutate", [
    lambda value: value.__setitem__(0, 0),
    lambda value: value.__setitem__(slice(None), []),
    lambda value: value.__delitem__(0),
    lambda value: value.__iadd__([3]),
    lambda value: value.__imul__(2),
    lambda value: value.append(3),
    lambda value: value.clear(),
    lambda value: value.extend([3]),
    lambda value: value.insert(0, 3),
    lambda value: value.pop(),
    lambda value: value.remove(1),
    lambda value: value.reverse(),
    lambda value: value.sort(),
])
def test_nested_lists_reject_mutation(scenario_data, mutate):
    catalog = freeze_catalog(scenario_data)
    nested = catalog[0]["extra"][3]["unknownField"]
    with pytest.raises(TypeError, match="immutable"):
        mutate(nested)
    assert catalog == scenario_data


@pytest.mark.parametrize("mutate", [
    lambda value: value.__setitem__("entries", 999),
    lambda value: value.__delitem__("entries"),
    lambda value: value.__ior__({"other": 1}),
    lambda value: value.clear(),
    lambda value: value.pop("entries"),
    lambda value: value.popitem(),
    lambda value: value.setdefault("other", 1),
    lambda value: value.update(entries=999),
])
def test_nested_dicts_reject_mutation(scenario_data, mutate):
    catalog = freeze_catalog(scenario_data)
    with pytest.raises(TypeError, match="immutable"):
        mutate(catalog[0]["counts"])
    assert catalog == scenario_data


def test_container_constructors_freeze_contents_and_cannot_reinitialize():
    source = {"nested": [1]}
    frozen_dict, frozen_list = FrozenDict(source), FrozenList([source])
    source["nested"].append(2)
    frozen_dict.__init__({"replacement": []})
    frozen_list.__init__(["replacement"])

    assert frozen_dict == {"nested": [1]}
    assert frozen_list == [{"nested": [1]}]
    with pytest.raises(TypeError):
        frozen_list[0]["nested"].append(3)


def test_unknown_mutable_values_cannot_be_shared_as_frozen():
    with pytest.raises(TypeError, match="Unsupported"):
        freeze_json({"mutable": set()})
    with pytest.raises(ValueError, match="must be a list"):
        freeze_catalog({})
    source = {"nested": []}
    frozen_tuple = freeze_json((source,))
    source["nested"].append(1)
    assert frozen_tuple == ({"nested": []},)


def test_loaded_cache_freezes_catalog_only(scenario_data):
    original = {"scenarios": scenario_data, "scores": {"42": {"user": {"score": 10}}}}
    assert cache.save_scores_cache(original)
    loaded = cache.load_scores_cache()

    assert loaded == original
    assert isinstance(loaded["scenarios"], FrozenList)
    with pytest.raises(TypeError, match="immutable"):
        loaded["scenarios"][0]["counts"]["entries"] = 999
    loaded["scores"]["42"]["user"]["score"] = 20
    assert cache.save_scores_cache(loaded)
    assert cache.load_scores_cache()["scores"]["42"]["user"]["score"] == 20


def test_app_checkpoint_shares_catalog_but_detaches_changing_fields(scenario_data):
    catalog = freeze_catalog(scenario_data)
    app = SimpleNamespace(_data_lock=threading.RLock(), _scores_cache={
        "scenarios": catalog,
        "scores": {"42": {"user": {"score": 10}, "friends": [{"score": 9}]}},
        "entry_history": {"42": CompactHistory({"2026-10-01": 250})},
        "local_stats": {"Example": {"recent_scores": [["2026-10-01", 10]]}},
    })
    snapshot = KovaaksAPI._cache_snapshot(app)
    assert snapshot["scenarios"] is catalog
    assert snapshot["scores"] is not app._scores_cache["scores"]
    assert snapshot["entry_history"]["42"] is not app._scores_cache["entry_history"]["42"]
    assert snapshot["local_stats"] is not app._scores_cache["local_stats"]

    app._scores_cache["scores"]["42"]["user"]["score"] = 20
    app._scores_cache["scores"]["42"]["friends"][0]["score"] = 19
    app._scores_cache["entry_history"]["42"]["2026-10-01"] = 500
    app._scores_cache["local_stats"]["Example"]["recent_scores"][0][1] = 20
    replacement = deepcopy(scenario_data)
    replacement[0]["counts"]["entries"] = 500
    app._scores_cache["scenarios"] = freeze_catalog(replacement)

    assert snapshot["scenarios"] == scenario_data
    assert snapshot["scores"]["42"] == {"user": {"score": 10}, "friends": [{"score": 9}]}
    assert dict(snapshot["entry_history"]["42"]) == {"2026-10-01": 250}
    assert snapshot["local_stats"]["Example"]["recent_scores"] == [["2026-10-01", 10]]
    assert cache.save_scores_cache(snapshot)
    assert cache.load_scores_cache()["scenarios"] == scenario_data


def test_download_freezes_catalog_before_recording_validator_identity(monkeypatch, scenario_data):
    response = requests.Response()
    response.status_code = 200
    response.headers["ETag"] = "version-one"
    response._content = gzip.compress(json.dumps(scenario_data).encode())
    response._content_consumed = True
    request = Mock(return_value=response)
    monkeypatch.setattr(fetch_worker, "api_request_with_retry", request)
    app = SimpleNamespace(_cfg={}, _scores_cache={})

    catalog = fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", app)
    assert isinstance(catalog, FrozenList)
    assert app._dataset_download_cache["scenarios.json.gz"].scenario_data is catalog
    app._scores_cache["scenarios"] = catalog
    fetch_worker.mark_dataset_applied(app, "scenarios.json.gz")
    unchanged = requests.Response()
    unchanged.status_code = 304
    unchanged._content = b""
    unchanged._content_consumed = True
    request.return_value = unchanged

    assert fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", app) is fetch_worker.DATASET_UNCHANGED
    assert request.call_args.kwargs["headers"] == {"If-None-Match": "version-one"}


def test_api_fallback_freezes_catalog_after_count_updates(monkeypatch, scenario_data):
    app = SimpleNamespace(
        _cfg={"min_entries": 10}, _scores_cache={"scenarios": [], "scores": {}},
        _fetch_cancelled=False, _data_lock=threading.RLock(),
        _update_status=lambda *args: None, _update_progress=lambda *args: None,
        _record_history_points=lambda *args: None, _rebuild_data=lambda: None,
        _rebuild_data_and_finish=lambda *args, **kwargs: None,
        _rebuild_data_and_cancelled=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(fetch_worker, "fetch_gzip_json_from_github", lambda *args: None)

    def fallback(**kwargs):
        scenario_data[0]["counts"]["entries"] = 300
        return scenario_data

    monkeypatch.setattr(fetch_worker, "fetch_all_scenarios", fallback)
    fetch_worker.run_fetch_all(app, "test_user", "")

    catalog = app._scores_cache["scenarios"]
    assert isinstance(catalog, FrozenList)
    assert catalog[0]["counts"]["entries"] == 300
    scenario_data[0]["counts"]["entries"] = 900
    assert catalog[0]["counts"]["entries"] == 300
