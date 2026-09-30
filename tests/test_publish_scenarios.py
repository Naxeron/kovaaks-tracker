"""Scenario release publication never mutates Git history or production data."""

import datetime
import gzip
import json
import subprocess
from unittest.mock import Mock, call

import pytest
import requests

from scripts import publish_scenarios as publisher


REPO = "owner/tracker"
SCENARIOS = [{"leaderboardId": 42, "counts": {"entries": 250}}]
HISTORY = {"timestamps": ["2026-09-30T12:00:00"], "history": {"42": [250]}}


def compressed(value):
    return gzip.compress(json.dumps(value).encode("utf-8"), mtime=0)


def release(**overrides):
    return {
        "tag_name": publisher.RELEASE_TAG,
        "draft": False,
        "prerelease": True,
        "immutable": False,
        "assets": [{"name": name} for name in publisher.ASSET_NAMES],
        **overrides,
    }


def response(status=200, data=None, content=b""):
    result = Mock(status_code=status, content=content)
    result.json.return_value = data
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    return result


def session_for(*responses):
    session = Mock()
    session.get.side_effect = responses
    return session


def write_datasets(output_dir, scenarios=SCENARIOS, history=HISTORY):
    output_dir.mkdir(parents=True, exist_ok=True)
    contents = {
        publisher.ASSET_NAMES[0]: compressed(scenarios),
        publisher.ASSET_NAMES[1]: compressed(history),
    }
    for name, content in contents.items():
        (output_dir / name).write_bytes(content)
    return contents


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # Tests never use developer or workflow credentials.
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)


def test_bootstrap_downloads_complete_release_pair(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    contents = [compressed(SCENARIOS), compressed(HISTORY)]
    session = session_for(
        response(data=release()), *(response(content=content) for content in contents),
    )

    publisher.bootstrap_datasets(REPO, tmp_path, session)

    assert [(tmp_path / name).read_bytes() for name in publisher.ASSET_NAMES] == contents
    calls = session.get.call_args_list
    assert calls[0] == call(
        f"https://api.github.com/repos/{REPO}/releases/tags/scenario-data",
        headers={"Accept": "application/vnd.github+json", "Authorization": "Bearer test-token"},
        timeout=publisher.REQUEST_TIMEOUT,
    )
    assert [item.args[0] for item in calls[1:]] == [
        publisher.release_asset_url(REPO, name) for name in publisher.ASSET_NAMES
    ]
    assert all("headers" not in item.kwargs for item in calls[1:])
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(publisher.ASSET_NAMES)


def test_first_bootstrap_uses_pinned_legacy_commit(tmp_path):
    session = session_for(
        response(404), response(content=compressed(SCENARIOS)), response(content=compressed(HISTORY)),
    )

    publisher.bootstrap_datasets(REPO, tmp_path, session)

    assert [item.args[0] for item in session.get.call_args_list[1:]] == [
        f"https://raw.githubusercontent.com/{REPO}/{publisher.LEGACY_SEED_REF}/data/{name}"
        for name in publisher.ASSET_NAMES
    ]


def test_first_bootstrap_accepts_explicit_seed_commit(tmp_path):
    seed_ref = "a" * 40
    session = session_for(
        response(404), response(content=compressed(SCENARIOS)), response(content=compressed(HISTORY)),
    )

    publisher.bootstrap_datasets(REPO, tmp_path, session, seed_ref=seed_ref)

    assert f"/{seed_ref}/data/" in session.get.call_args_list[1].args[0]


def test_unpinned_seed_ref_is_rejected(tmp_path):
    session = session_for(response(404))
    with pytest.raises(ValueError, match="full commit SHA"):
        publisher.bootstrap_datasets(REPO, tmp_path, session, seed_ref="main")
    assert session.get.call_count == 1


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_metadata_errors_never_trigger_legacy_fallback(tmp_path, status):
    originals = write_datasets(tmp_path)
    session = session_for(response(status))

    with pytest.raises(requests.HTTPError):
        publisher.bootstrap_datasets(REPO, tmp_path, session)

    assert session.get.call_count == 1
    assert {name: (tmp_path / name).read_bytes() for name in publisher.ASSET_NAMES} == originals


def test_partial_release_never_falls_back_to_legacy(tmp_path):
    session = session_for(response(data=release(assets=[{"name": publisher.ASSET_NAMES[0]}])))

    with pytest.raises(ValueError, match="missing assets"):
        publisher.bootstrap_datasets(REPO, tmp_path, session)

    assert session.get.call_count == 1
    assert list(tmp_path.iterdir()) == []


def test_corrupt_second_download_preserves_both_existing_files(tmp_path):
    originals = write_datasets(tmp_path)
    updated = [{"leaderboardId": 42, "counts": {"entries": 500}}]
    session = session_for(
        response(data=release()), response(content=compressed(updated)), response(content=b"truncated gzip"),
    )

    with pytest.raises(ValueError, match="Invalid dataset"):
        publisher.bootstrap_datasets(REPO, tmp_path, session)

    assert {name: (tmp_path / name).read_bytes() for name in publisher.ASSET_NAMES} == originals


def test_download_network_error_preserves_existing_pair(tmp_path):
    originals = write_datasets(tmp_path)
    session = session_for(response(data=release()), requests.ConnectionError("offline"))

    with pytest.raises(requests.ConnectionError):
        publisher.bootstrap_datasets(REPO, tmp_path, session)

    assert {name: (tmp_path / name).read_bytes() for name in publisher.ASSET_NAMES} == originals


def test_release_asset_404_is_retried_briefly(tmp_path, monkeypatch):
    sleep = Mock()
    monkeypatch.setattr(publisher.time, "sleep", sleep)
    session = session_for(
        response(data=release()), response(404), response(content=compressed(SCENARIOS)),
        response(content=compressed(HISTORY)),
    )

    publisher.bootstrap_datasets(REPO, tmp_path, session)

    assert session.get.call_count == 4
    sleep.assert_called_once_with(2)


def test_release_asset_retry_is_bounded_and_never_falls_back(tmp_path, monkeypatch):
    sleep = Mock()
    monkeypatch.setattr(publisher.time, "sleep", sleep)
    session = session_for(response(data=release()), response(404), response(404), response(404))

    with pytest.raises(requests.HTTPError):
        publisher.bootstrap_datasets(REPO, tmp_path, session)

    assert session.get.call_count == 4
    assert sleep.call_args_list == [call(2), call(2)]
    assert all("raw.githubusercontent.com" not in item.args[0] for item in session.get.call_args_list)
    assert list(tmp_path.iterdir()) == []


def test_missing_seed_fails_instead_of_bootstrapping_empty_data(tmp_path, monkeypatch):
    sleep = Mock()
    monkeypatch.setattr(publisher.time, "sleep", sleep)
    session = session_for(response(404), response(404))

    with pytest.raises(requests.HTTPError):
        publisher.bootstrap_datasets(REPO, tmp_path, session)

    assert session.get.call_count == 2
    sleep.assert_not_called()


@pytest.mark.parametrize("value", [
    [], {}, [{"leaderboardId": 42}],
    [{"leaderboardId": 42, "counts": {"entries": "250"}}],
    [{"leaderboardId": 42, "counts": {"entries": -1}}],
    [{"leaderboardId": 42, "counts": {"entries": True}}],
])
def test_rejects_unusable_scenario_datasets(value):
    with pytest.raises(ValueError, match="Invalid dataset structure"):
        publisher.validate_dataset(publisher.ASSET_NAMES[0], compressed(value))


@pytest.mark.parametrize("value", [
    [], {}, {"timestamps": "invalid", "history": {}},
    {"timestamps": [None], "history": {}},
    {"timestamps": ["2026-09-30"], "history": {"42": []}},
    {"timestamps": ["2026-09-30"], "history": {"42": 42}},
])
def test_rejects_unusable_history_datasets(value):
    with pytest.raises(ValueError, match="Invalid dataset structure"):
        publisher.validate_dataset(publisher.ASSET_NAMES[1], compressed(value))


@pytest.mark.parametrize("content", [b"not gzip", compressed(SCENARIOS)[:-4], gzip.compress(b"not JSON")])
def test_rejects_corrupt_compressed_json(content):
    with pytest.raises(ValueError, match="Invalid dataset"):
        publisher.validate_dataset(publisher.ASSET_NAMES[0], content)


def test_publish_creates_one_prerelease_with_both_assets(tmp_path, monkeypatch):
    write_datasets(tmp_path)
    session = session_for(response(404))
    run_gh = Mock()
    monkeypatch.setattr(publisher, "_run_gh", run_gh)

    assert publisher.publish_datasets(REPO, tmp_path, session) is True

    arguments = run_gh.call_args.args[0]
    assert arguments[:3] == ["release", "create", "scenario-data"]
    assert all(str(tmp_path / name) in arguments for name in publisher.ASSET_NAMES)
    assert "--prerelease" in arguments
    assert "--latest=false" in arguments
    assert arguments[arguments.index("--repo") + 1] == REPO
    run_gh.assert_called_once()


def test_publish_replaces_only_changed_asset(tmp_path, monkeypatch):
    updated_history = {"timestamps": ["2026-09-30T12:05:00"], "history": {"42": [250]}}
    write_datasets(tmp_path, history=updated_history)
    session = session_for(
        response(data=release()), response(content=compressed(SCENARIOS)), response(content=compressed(HISTORY)),
    )
    run_gh = Mock()
    monkeypatch.setattr(publisher, "_run_gh", run_gh)

    assert publisher.publish_datasets(REPO, tmp_path, session) is True

    run_gh.assert_called_once_with([
        "release", "upload", "scenario-data", str(tmp_path / publisher.ASSET_NAMES[1]),
        "--repo", REPO, "--clobber",
    ])


def test_publish_skips_identical_datasets(tmp_path, monkeypatch):
    contents = write_datasets(tmp_path)
    session = session_for(
        response(data=release()), *(response(content=contents[name]) for name in publisher.ASSET_NAMES),
    )
    run_gh = Mock()
    monkeypatch.setattr(publisher, "_run_gh", run_gh)

    assert publisher.publish_datasets(REPO, tmp_path, session) is False
    run_gh.assert_not_called()


@pytest.mark.parametrize("overrides,message", [
    ({"immutable": True}, "immutable"),
    ({"prerelease": False}, "prerelease"),
    ({"draft": True}, "draft"),
])
def test_publish_refuses_incompatible_release(tmp_path, monkeypatch, overrides, message):
    write_datasets(tmp_path)
    session = session_for(response(data=release(**overrides)))
    run_gh = Mock()
    monkeypatch.setattr(publisher, "_run_gh", run_gh)

    with pytest.raises(ValueError, match=message):
        publisher.publish_datasets(REPO, tmp_path, session)

    assert session.get.call_count == 1
    run_gh.assert_not_called()


def test_publish_refuses_corrupt_local_history_before_remote_access(tmp_path, monkeypatch):
    write_datasets(tmp_path)
    (tmp_path / publisher.ASSET_NAMES[1]).write_bytes(b"broken")
    session = Mock()
    run_gh = Mock()
    monkeypatch.setattr(publisher, "_run_gh", run_gh)

    with pytest.raises(ValueError, match="Invalid dataset"):
        publisher.publish_datasets(REPO, tmp_path, session)

    session.get.assert_not_called()
    run_gh.assert_not_called()


def test_publish_refuses_corrupt_remote_data_instead_of_overwriting(tmp_path, monkeypatch):
    write_datasets(tmp_path)
    session = session_for(response(data=release()), response(content=b"broken"))
    run_gh = Mock()
    monkeypatch.setattr(publisher, "_run_gh", run_gh)

    with pytest.raises(ValueError, match="Invalid dataset"):
        publisher.publish_datasets(REPO, tmp_path, session)

    run_gh.assert_not_called()


def test_upload_failure_is_reported(tmp_path, monkeypatch):
    write_datasets(tmp_path)
    session = session_for(response(404))
    run = Mock(side_effect=subprocess.CalledProcessError(1, ["gh"], stderr="permission denied"))
    monkeypatch.setattr(publisher.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="permission denied"):
        publisher.publish_datasets(REPO, tmp_path, session)

    assert run.call_args.args[0][:3] == ["gh", "release", "create"]
    assert run.call_args.kwargs["check"] is True
    assert run.call_args.kwargs["timeout"] == 180


def test_repository_validation_prevents_malformed_api_urls(tmp_path):
    session = Mock()
    with pytest.raises(ValueError, match="OWNER/REPO"):
        publisher.bootstrap_datasets("../tracker?token=bad", tmp_path, session)
    session.get.assert_not_called()


def test_cli_returns_nonzero_for_bootstrap_error(tmp_path, monkeypatch, caplog):
    session = session_for(response(503))
    context = Mock()
    context.__enter__ = Mock(return_value=session)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(publisher.requests, "Session", Mock(return_value=context))

    assert publisher.main(["bootstrap", "--repo", REPO, "--output-dir", str(tmp_path)]) == 1
    assert "bootstrap failed" in caplog.text


def test_cli_reads_repository_environment_and_passes_seed_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    session = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=session)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(publisher.requests, "Session", Mock(return_value=context))
    bootstrap = Mock()
    monkeypatch.setattr(publisher, "bootstrap_datasets", bootstrap)
    seed_ref = "a" * 40

    assert publisher.main(["bootstrap", "--output-dir", str(tmp_path), "--seed-ref", seed_ref]) == 0
    bootstrap.assert_called_once_with(REPO, tmp_path, session, seed_ref)


def test_failed_changed_asset_upload_keeps_generated_pair_for_recovery(tmp_path, monkeypatch, caplog):
    updated_scenarios = [{"leaderboardId": 42, "counts": {"entries": 300}}]
    updated_history = {"timestamps": ["2026-09-30T12:05:00"], "history": {"42": [300]}}
    generated = write_datasets(tmp_path, scenarios=updated_scenarios, history=updated_history)
    session = session_for(
        response(data=release()), response(content=compressed(SCENARIOS)), response(content=compressed(HISTORY)),
    )
    context = Mock()
    context.__enter__ = Mock(return_value=session)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(publisher.requests, "Session", Mock(return_value=context))
    run = Mock(side_effect=subprocess.CalledProcessError(
        1, ["gh"], stderr="upload failed after deleting prior asset",
    ))
    monkeypatch.setattr(publisher.subprocess, "run", run)

    assert publisher.main(["publish", "--repo", REPO, "--output-dir", str(tmp_path)]) == 1

    assert "upload failed after deleting prior asset" in caplog.text
    assert run.call_args.args[0] == [
        "gh", "release", "upload", "scenario-data",
        *(str(tmp_path / name) for name in publisher.ASSET_NAMES),
        "--repo", REPO, "--clobber",
    ]
    assert {name: (tmp_path / name).read_bytes() for name in publisher.ASSET_NAMES} == generated
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(publisher.ASSET_NAMES)


def test_bootstrap_fetch_and_publish_preserves_history_and_uses_accurate_counts(tmp_path, monkeypatch):
    from scripts import fetch_scenarios

    previous_scenarios = [*SCENARIOS, {"leaderboardId": 7, "counts": {"entries": 90}}]
    previous_history = {
        "timestamps": ["2026-09-30T10:00:00"], "history": {"42": [250], "7": [90]},
    }
    old_contents = [compressed(previous_scenarios), compressed(previous_history)]
    network = session_for(
        response(data=release()), *(response(content=content) for content in old_contents),
        response(data=release()), *(response(content=content) for content in old_contents),
    )
    page = response(data={"data": [
        {"leaderboardId": 42, "counts": {"entries": 900000}},
        {"leaderboardId": 43, "counts": {"entries": 500000}},
    ], "total": 2})
    request = Mock(return_value=page)
    accurate = Mock(side_effect=lambda lid, session: {42: 275, 43: 50}[lid])
    monkeypatch.setattr(fetch_scenarios, "api_request_with_retry", request)
    monkeypatch.setattr(fetch_scenarios, "get_accurate_entry_count", accurate)
    monkeypatch.setattr(fetch_scenarios.time, "sleep", Mock())
    run = Mock()
    monkeypatch.setattr(publisher.subprocess, "run", run)

    publisher.bootstrap_datasets(REPO, tmp_path, network)
    assert fetch_scenarios.generate_datasets(
        tmp_path, entries_limit=10, now=datetime.datetime(2026, 9, 30, 12),
    ) is True
    assert publisher.publish_datasets(REPO, tmp_path, network) is True

    generated = {
        name: publisher.validate_dataset(name, (tmp_path / name).read_bytes())
        for name in publisher.ASSET_NAMES
    }
    assert generated[publisher.ASSET_NAMES[0]] == [
        {"leaderboardId": 42, "counts": {"entries": 275}},
        {"leaderboardId": 7, "counts": {"entries": 90}},
        {"leaderboardId": 43, "counts": {"entries": 50}},
    ]
    assert generated[publisher.ASSET_NAMES[1]] == {
        "timestamps": ["2026-09-30T10:00:00", "2026-09-30T12:00:00"],
        "history": {"42": [250, 275], "7": [90, 90], "43": [None, 50]},
    }
    request.assert_called_once()
    assert request.call_args.kwargs["params"] == {"page": 0, "max": 100}
    assert accurate.call_count == 2
    assert {item.args[0] for item in accurate.call_args_list} == {42, 43}
    run.assert_called_once_with([
        "gh", "release", "upload", "scenario-data",
        *(str(tmp_path / name) for name in publisher.ASSET_NAMES),
        "--repo", REPO, "--clobber",
    ], check=True, capture_output=True, text=True, timeout=180)
