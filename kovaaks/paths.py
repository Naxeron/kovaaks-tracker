"""Runtime paths and lazy migration of files from older tracker releases."""

import os


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")


def existing_data_path(destination):
    """Prefer the data file, otherwise find its legacy sibling in the root.

    Derive the legacy location from the requested destination, rather than a
    fixed project path, so custom paths and temporary test directories remain
    isolated. Only files directly inside a ``data`` directory use migration.
    """
    destination = os.fspath(destination)
    directory = os.path.dirname(destination)
    if os.path.lexists(destination) or os.path.basename(directory) != "data":
        return destination
    legacy = os.path.join(os.path.dirname(directory), os.path.basename(destination))
    return legacy if os.path.lexists(legacy) else destination


def migrate_legacy_file(source, destination):
    """Move a legacy file without ever replacing an existing destination.

    A hard link publishes the complete file atomically, preserving permissions
    and avoiding a partly copied config after an interrupted write. Both paths
    belong to the same installation. Unsupported filesystems or permissions
    raise ``OSError`` so callers can keep using the original file this session.
    """
    if os.path.islink(source):
        # Moving a relative symlink would change the file it points to.
        raise OSError("Cannot migrate a symbolic link; keeping the original path")
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    try:
        os.link(source, destination)
    except FileExistsError:
        return destination
    except FileNotFoundError:
        # Another process may have completed this same migration already.
        if os.path.lexists(destination):
            return destination
        raise
    try:
        os.unlink(source)
    except OSError:
        # Keep the original authoritative on failure, without leaving a second
        # settings file that might retain a password after credential migration.
        if os.path.samefile(source, destination):
            os.unlink(destination)
        raise
    return destination
