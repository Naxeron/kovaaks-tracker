# Development and releases

## Source checkout

```bash
git clone https://github.com/naxeron/kovaaks-tracker.git
cd kovaaks-tracker
python -m pip install -r requirements/dev.txt
python kovaaks_web.py
```

Use a virtual environment; see [installation instructions](INSTALL.md) for
activation and Linux desktop dependencies. The root `requirements.txt` remains
as a compatibility entry point for existing install commands.

## Layout

```text
kovaaks_web.py            Desktop launcher
kovaaks/                  Application code
  app.py                  Desktop API and window lifecycle
  web/                    HTML, JavaScript, CSS
requirements/             Runtime and development dependencies
docs/                     User/developer guides and screenshots
scripts/                  Dataset publishing and distribution tools
scratch/                  Exploratory analysis; not needed by the app
tests/                    Headless regression suite
.github/workflows/        CI, application builds, scenario publishing
data/                     Local settings, logs, caches (not distributed)
dist/                     Built ZIPs (not committed)
```

Keep runtime code and resources under `kovaaks/`. Runtime dependencies belong in
`requirements/runtime.txt`; development dependencies belong in `requirements/dev.txt`.
Essential project metadata stays at the source root so GitHub, pytest, and
contributors can discover it. Tests are retained in the repository but excluded
from application downloads.

## Build an application download

```bash
python scripts/build_distribution.py
```

The standard-library-only builder creates `dist/kovaaks-tracker.zip`. You can
choose another path with `--output /path/to/kovaaks-tracker.zip`. Each ZIP has one
`kovaaks-tracker/` folder with only `kovaaks_web.py` at its root, plus the runtime
package, `requirements/runtime.txt`, `docs/README.md`, and `docs/LICENSE`.

The builder includes only direct `kovaaks/*.py` modules and explicitly listed
web assets and supporting files. If you add a new runtime resource or Python
subpackage, update the builder's allowlist and distribution tests. It excludes
all caches, credentials, logs, virtual environments, tests, scratch tools, and
screenshots. Inputs must be ordinary files, not symlinks. Builds use stable ZIP
metadata and atomically replace the output only after writing succeeds.

The **Build Application** workflow runs the tests and builds this ZIP. Run it
manually to obtain an Actions artifact (extract the artifact wrapper to find the
application ZIP). After this workflow is on GitHub, publishing an application
release whose tag includes these changes also attaches `kovaaks-tracker.zip` to
that release. The rolling `scenario-data` release is explicitly excluded.
For an older release tag without this workflow, build locally and attach the ZIP
manually. Publishing/re-running an application workflow never updates scenario
data assets.

Users should download the attached application ZIP. Clones and GitHub's automatic
source archives still contain the full development project. Until the first
application asset is published, build locally or use a manual workflow artifact.

Generated dataset snapshots have been removed from Git history. If you cloned
before that cleanup, make a fresh clone to benefit from the reduced size. Keep
your local settings and cached scores when moving to it.

## Scenario data

The tracker downloads `scenarios.json.gz` and `scenarios_history.json.gz` from the
[Scenario data prerelease](https://github.com/Naxeron/kovaaks-tracker/releases/tag/scenario-data).
The scheduled workflow refreshes these two assets in place, without committing
generated files to the repository. Unchanged datasets are not uploaded again.
Scenario history retains up to 168 hourly samples.

The local scores cache shares timestamps and packs history counts to reduce
memory use. Existing caches load automatically and switch formats on the next
normal cache save. The first load of an old cache can still reach its previous
memory peak; restarting after that save gives the smaller startup footprint.
Tools reading local history should use `kovaaks.cache.load_scores_cache()` to
handle both formats. The downloaded release datasets keep their existing format.
Loaded scenario catalogs are immutable so cache checkpoints can share their
metadata. Changing scores, history, and local statistics still get independent
snapshots. Replace a catalog as a whole when installing updated scenario data.

During refreshes, downloads are decoded incrementally and history counts stay
packed in memory. On Linux, the tracker also returns unused heap pages after
cache loading, history merging, and background saves where the allocator supports
it. `Memory [...]` log entries report current and peak RAM for the Python process
at these stages; renderer processes are separate. These measurements help diagnose
growth during a long running session as well as temporary download peaks.

If downloads fail, the tracker can fetch scenarios directly from the KovaaKs API.
If neither source returns scenarios, it keeps the previous cache. Update older
tracker checkouts to use release downloads; their former raw GitHub URLs will no
longer provide the shared datasets after this migration.

To generate data locally, preserving the previous published history:

```bash
python scripts/publish_scenarios.py bootstrap --repo Naxeron/kovaaks-tracker --output-dir data
python scripts/fetch_scenarios.py --min-entries 10 --output-dir data
```

The generated files are ignored by Git. Bootstrap requires both valid release
assets; an incomplete or corrupt release fails instead of resetting history.
The release was seeded before removing dataset snapshots from Git history.
The pinned legacy seed is only for the initial migration and may be unavailable
after history cleanup. Keep the release intact and use the recovery procedure
below to restore damaged assets.

GitHub replaces release assets individually. Publishing failures preserve the
successfully generated pair as a `scenario-data-recovery` workflow artifact for
seven days. To repair a missing or corrupt asset, download and extract that
artifact, then validate and restore both files with the authenticated GitHub CLI:

```bash
python - <<'PY'
from pathlib import Path
from scripts.publish_scenarios import ASSET_NAMES, validate_dataset
for name in ASSET_NAMES:
    validate_dataset(name, (Path("recovery") / name).read_bytes())
PY
gh release upload scenario-data recovery/scenarios.json.gz recovery/scenarios_history.json.gz --repo Naxeron/kovaaks-tracker --clobber
```

Keep the data release mutable so its assets can be replaced. It is a prerelease
and does not take the place of the latest application release.

## Tests

```bash
python -m pip install -r requirements/dev.txt
python -m pytest -q
```

Tests use temporary caches, settings, logs, and stats directories, plus fake
credential stores that never access your system keychain. Install Node.js
to include the JavaScript rendering tests; those tests are skipped when Node.js
is unavailable. GitHub Actions runs the suite on Python 3.10 and 3.14 for pushes
and pull requests.
CI also validates workflow syntax and expression contexts with actionlint.

