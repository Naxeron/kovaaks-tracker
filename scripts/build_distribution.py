#!/usr/bin/env python3
"""Build a small, reproducible application ZIP without developer or local data.

Only the runtime package, launcher, dependency list, and user documentation are
included. Building needs only Python's standard library, not Git or app imports.
"""

import argparse
import os
from pathlib import Path
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


PROJECT_DIR = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = "kovaaks-tracker"
REQUIRED_FILES = {
    "kovaaks_web.py": "kovaaks_web.py",
    "kovaaks/__init__.py": "kovaaks/__init__.py",
    "kovaaks/app.py": "kovaaks/app.py",
    "kovaaks/web/index.html": "kovaaks/web/index.html",
    "kovaaks/web/script.js": "kovaaks/web/script.js",
    "kovaaks/web/style.css": "kovaaks/web/style.css",
    "requirements/runtime.txt": "requirements/runtime.txt",
    "docs/INSTALL.md": "docs/README.md",
    "LICENSE": "docs/LICENSE",
}


def _distribution_files(source_dir):
    """Return the allowlist, rejecting missing files and linked source paths."""
    files = dict(REQUIRED_FILES)
    files.update({
        path.relative_to(source_dir).as_posix(): path.relative_to(source_dir).as_posix()
        for path in (source_dir / "kovaaks").glob("*.py")
    })
    for relative in files:
        path = source_dir / relative
        # Check ancestors too: a linked package/assets directory could pull
        # unrelated files from outside this checkout into an otherwise valid ZIP.
        if any(
            part.is_symlink() for part in (path, *path.parents)
            if source_dir in part.parents
        ):
            raise ValueError(f"Distribution source must not be a symlink: {relative}")
        if not path.is_file():
            raise FileNotFoundError(f"Missing distribution source: {relative}")
    return files


def build_distribution(output, source_dir=None):
    """Atomically write a runtime ZIP, leaving any previous build intact on error."""
    source_dir = Path(source_dir or PROJECT_DIR).resolve()
    output = Path(output).absolute()
    files = _distribution_files(source_dir)
    if output.resolve() in {(source_dir / relative).resolve() for relative in files}:
        raise ValueError("Distribution output must not replace a source file")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output.parent, prefix=f".{output.name}.{os.getpid()}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, destination in sorted(files.items(), key=lambda item: item[1]):
                # Fixed timestamps and permissions make builds independent of
                # local mtimes and umask. Never include caches or generated data.
                info = ZipInfo(f"{ARCHIVE_ROOT}/{destination}", date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100755 if destination == "kovaaks_web.py" else 0o100644) << 16
                info.compress_type = ZIP_DEFLATED
                archive.writestr(info, (source_dir / relative).read_bytes(), compresslevel=9)
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def main():
    """Build from this checkout regardless of the caller's working directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=PROJECT_DIR / "dist" / "kovaaks-tracker.zip",
        help="Destination ZIP (default: dist/kovaaks-tracker.zip in this checkout)",
    )
    args = parser.parse_args()
    try:
        output = build_distribution(args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Could not build application ZIP: {exc}\n")
    print(f"Built {output} ({output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
