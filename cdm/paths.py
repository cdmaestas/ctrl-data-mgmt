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
from pathlib import Path

APP_NAME = "ctrl-data-mgmt"


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
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass  # a directory we cannot chmod is still usable; the db mode is what matters
    return d


def this_host() -> str:
    """The host dimension of every row.

    v1 only ever scans locally, but the column exists from the start so that a
    later pdsh-fanned scan is a merge of per-host indexes rather than a schema
    migration against an index you have come to rely on.
    """
    override = env("HOST")
    return override or socket.gethostname()
