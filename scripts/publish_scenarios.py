#!/usr/bin/env python3
"""Keep the latest scenario datasets in one replaceable GitHub prerelease.

Bootstrap both previous datasets before fetching so a missing or corrupt history
cannot silently become an empty replacement. No dataset snapshots are committed.
"""

import argparse
import gzip
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import zlib

import requests


RELEASE_TAG = "scenario-data"
ASSET_NAMES = ("scenarios.json.gz", "scenarios_history.json.gz")
# Migration-only seed; history cleanup may make this commit unavailable.
# Normal runs restore the existing release, which must be kept intact.
LEGACY_SEED_REF = "bea7dcdcd815850d0441753bdfb184cd5cb63f7e"
REQUEST_TIMEOUT = 60
logger = logging.getLogger("publish_scenarios")


def _validate_repo(repo):
    """Accept a GitHub owner/repository, keeping constructed URLs unambiguous."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Repository must have the form OWNER/REPO")


def release_asset_url(repo, name):
    """Return the stable public URL consumed by the application."""
    return f"https://github.com/{repo}/releases/download/{RELEASE_TAG}/{name}"


def get_release(repo, session):
    """Return release metadata; only an explicit 404 means no release exists."""
    _validate_repo(repo)
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = session.get(
        f"https://api.github.com/repos/{repo}/releases/tags/{RELEASE_TAG}",
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    release = response.json()
    if (
        not isinstance(release, dict)
        or release.get("tag_name") != RELEASE_TAG
        or not isinstance(release.get("assets"), list)
    ):
        raise ValueError("Unexpected scenario release metadata")
    if release.get("draft"):
        raise ValueError("Scenario data release is still a draft")
    return release


def validate_dataset(name, content):
    """Reject truncated gzip, invalid JSON, and unusable dataset structures."""
    try:
        data = json.loads(gzip.decompress(content))
    except (OSError, EOFError, UnicodeError, ValueError, zlib.error) as exc:
        raise ValueError(f"Invalid dataset {name}: {exc}") from exc

    if name == ASSET_NAMES[0]:
        valid = isinstance(data, list) and bool(data) and all(
            isinstance(item, dict)
            and bool(item.get("leaderboardId"))
            and isinstance(item.get("counts"), dict)
            and isinstance(item["counts"].get("entries"), (int, float))
            and not isinstance(item["counts"]["entries"], bool)
            and item["counts"]["entries"] >= 0
            for item in data
        )
    elif name == ASSET_NAMES[1]:
        valid = (
            isinstance(data, dict)
            and isinstance(data.get("timestamps"), list)
            and all(isinstance(timestamp, str) for timestamp in data["timestamps"])
            and isinstance(data.get("history"), dict)
            and all(
                isinstance(values, list)
                and len(values) == len(data["timestamps"])
                for values in data["history"].values()
            )
        )
    else:
        raise ValueError(f"Unknown dataset {name}")
    if not valid:
        raise ValueError(f"Invalid dataset structure: {name}")
    return data


def _download(url, name, session, retry_missing=False):
    # Do not send the API token to public downloads or their redirect targets.
    for attempt in range(3):
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        if not retry_missing or response.status_code != 404 or attempt == 2:
            break
        # gh --clobber deletes an asset immediately before uploading its successor.
        time.sleep(2)
    response.raise_for_status()
    content = response.content
    validate_dataset(name, content)
    return content


def _asset_names(release):
    return {
        asset.get("name")
        for asset in release.get("assets", [])
        if isinstance(asset, dict)
    }


def bootstrap_datasets(repo, output_dir, session, seed_ref=LEGACY_SEED_REF):
    """Download and validate the complete previous pair before installing it.

    Pinned legacy snapshots are used only during the first migration, while the
    data release does not exist. A damaged release must be repaired explicitly.
    """
    release = get_release(repo, session)
    if release is not None:
        missing = set(ASSET_NAMES) - _asset_names(release)
        if missing:
            raise ValueError(f"Scenario release is missing assets: {', '.join(sorted(missing))}")
        urls = {name: release_asset_url(repo, name) for name in ASSET_NAMES}
    else:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", seed_ref):
            raise ValueError("Legacy seed reference must be a full commit SHA")
        urls = {
            name: f"https://raw.githubusercontent.com/{repo}/{seed_ref}/data/{name}"
            for name in ASSET_NAMES
        }
    contents = {
        name: _download(urls[name], name, session, retry_missing=release is not None)
        for name in ASSET_NAMES
    }

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Unique process-specific staging avoids concurrent atomic-save collisions.
    with tempfile.TemporaryDirectory(prefix=f".scenario-data-{os.getpid()}-", dir=output_dir) as stage:
        for name, content in contents.items():
            (Path(stage) / name).write_bytes(content)
        for name in ASSET_NAMES:
            os.replace(Path(stage) / name, output_dir / name)
    logger.info("Bootstrapped scenario datasets from %s", "release" if release else "pinned legacy snapshots")


def _run_gh(arguments):
    """Run the authenticated GitHub CLI, failing visibly on upload errors."""
    try:
        subprocess.run(
            ["gh", *arguments], check=True, capture_output=True, text=True,
            timeout=180,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"GitHub CLI failed: {exc.stderr.strip()}") from exc


def publish_datasets(repo, output_dir, session):
    """Create one prerelease or replace its changed assets, keeping storage bounded.

    GitHub replaces release assets individually. Workflow concurrency must
    serialize publishers; clients retain their cached data during replacement.
    """
    _validate_repo(repo)
    output_dir = Path(output_dir)
    contents = {name: (output_dir / name).read_bytes() for name in ASSET_NAMES}
    for name, content in contents.items():
        validate_dataset(name, content)

    release = get_release(repo, session)
    paths = [str(output_dir / name) for name in ASSET_NAMES]
    if release is None:
        _run_gh([
            "release", "create", RELEASE_TAG, *paths, "--repo", repo,
            "--prerelease", "--latest=false", "--title", "Scenario data",
            "--notes", "Automatically refreshed scenario datasets. Assets are replaced in place to avoid growing Git history.",
        ])
        logger.info("Created scenario data prerelease")
        return True

    if release.get("immutable"):
        raise ValueError("Scenario data release is immutable; asset replacement must be allowed")
    if not release.get("prerelease"):
        raise ValueError("Scenario data release must be a prerelease")

    existing = _asset_names(release)
    changed = [
        name for name in ASSET_NAMES
        if name not in existing
        or _download(release_asset_url(repo, name), name, session, retry_missing=True) != contents[name]
    ]
    if not changed:
        logger.info("Scenario datasets are unchanged")
        return False
    _run_gh([
        "release", "upload", RELEASE_TAG,
        *(str(output_dir / name) for name in changed),
        "--repo", repo, "--clobber",
    ])
    logger.info("Replaced %d scenario dataset assets", len(changed))
    return True


def main(argv=None):
    """Bootstrap before fetching, then publish only after the fetch succeeds."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("bootstrap", "publish"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed-ref", default=LEGACY_SEED_REF,
                        help="Full commit SHA containing the final legacy datasets")
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        with requests.Session() as session:
            if args.command == "bootstrap":
                bootstrap_datasets(args.repo, args.output_dir, session, args.seed_ref)
            else:
                publish_datasets(args.repo, args.output_dir, session)
    except (OSError, ValueError, requests.RequestException, RuntimeError, subprocess.TimeoutExpired) as exc:
        logger.error("Scenario data %s failed: %s", args.command, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
