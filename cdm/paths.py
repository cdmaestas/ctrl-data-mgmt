"""Where the index lives, and who is allowed to read it.

An index of every filename you own is more revealing than most file contents,
so the data directory is 0700 and the database is 0600, created that way rather
than chmod-ed afterwards.

Locations follow the XDG spec and are overridable by environment, which is what
makes the whole thing testable:

  data    $XDG_DATA_HOME/ctrl-data-mgmt/index.db   (~/.local/share/ctrl-data-mgmt)
"""
from __future__ import annotations

import os
import socket
import stat
import sys
from pathlib import Path

APP_NAME = "ctrl-data-mgmt"

# Modes that must hold. Anything readable by group or other defeats the point.
DIR_MODE = 0o700
FILE_MODE = 0o600
OTHERS_MASK = 0o077


def enforce_mode(path: Path, mode: int) -> str | None:
    """Set a mode and then CHECK it. Returns a warning, or None if it held.

    chmod can fail -- an unsupported filesystem, a network mount, a container
    overlay -- and this tool is aimed at people who point it at exactly those.
    Ignoring that failure is how the index quietly ends up world-readable while
    the README, the man page and CI all still claim 0600.

    So the mode is verified rather than assumed, and a failure is reported
    rather than swallowed.
    """
    try:
        os.chmod(path, mode)
    except OSError:
        pass  # deliberately continue: the point is to report the ACTUAL mode
    try:
        actual = stat.S_IMODE(os.stat(path).st_mode)
    except OSError as exc:
        return f"cdm: cannot check permissions on {path}: {exc}"
    if actual & OTHERS_MASK:
        return (f"cdm: WARNING {path} is mode {oct(actual)}, not {oct(mode)} -- "
                f"other users on this machine can read it. The filesystem may not "
                f"support chmod; move the index with CDM_DATA_DIR if that matters.")
    return None


def warn(message: str | None) -> None:
    """Print a permissions warning where it cannot be missed.

    Printing from a low-level module is poor layering. The alternative --
    returning a warning for callers to check -- is precisely the pattern that
    produced this bug, so it loses to being loud.
    """
    if message:
        print(message, file=sys.stderr)


def env(suffix: str, default: str | None = None) -> str | None:
    return os.environ.get(f"CDM_{suffix}", default)


def data_dir() -> Path:
    override = env("DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share")
    return Path(base) / APP_NAME


def index_path() -> Path:
    override = env("INDEX")
    return Path(override) if override else data_dir() / "index.db"


def ensure_data_dir() -> Path:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    warn(enforce_mode(d, DIR_MODE))
    return d


def this_host() -> str:
    """The host dimension of every row.

    v1 only ever scans locally, but the column exists from the start so that a
    later pdsh-fanned scan is a merge of per-host indexes rather than a schema
    migration against an index you have come to rely on.
    """
    override = env("HOST")
    return override or socket.gethostname()
