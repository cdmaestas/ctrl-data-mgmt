"""The index: schema, connection, migration.

One SQLite file. Two tables.

`files.hash_kind` is the load-bearing column. A partial hash (see hashing.py)
answers "are these probably the same file" for a thousandth of the I/O; a full
hash answers "is this byte-for-byte what I recorded". Storing which kind is in
the row makes it impossible to read one as the other later.

`hash_size` and `hash_mtime` record what the hash was computed *against*. A row
whose current size/mtime differ from those is a stale hash, and stale is a
state you can detect rather than a wrong answer you cannot.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import paths

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    host       TEXT NOT NULL,
    path       TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    last_scan  TEXT,
    PRIMARY KEY (host, path)
);

CREATE TABLE IF NOT EXISTS files (
    host       TEXT NOT NULL,
    root       TEXT NOT NULL,
    path       TEXT NOT NULL,
    parent     TEXT NOT NULL,
    name       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime      REAL NOT NULL,
    ctime      REAL NOT NULL,
    inode      INTEGER,
    type       TEXT NOT NULL,
    hash       TEXT,
    hash_kind  TEXT,
    hash_size  INTEGER,
    hash_mtime REAL,
    seen_at    TEXT NOT NULL,
    PRIMARY KEY (host, path)
);

CREATE INDEX IF NOT EXISTS idx_files_size  ON files(size);
CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime);
CREATE INDEX IF NOT EXISTS idx_files_name  ON files(name);
CREATE INDEX IF NOT EXISTS idx_files_hash  ON files(hash_kind, hash, size);
CREATE INDEX IF NOT EXISTS idx_files_root  ON files(host, root);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the index, at 0600."""
    db = Path(path) if path else paths.index_path()
    if db.parent != Path("."):
        db.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(db.parent, 0o700)
        except OSError:
            pass

    existed = db.exists()
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    if not existed:
        # Create the file with a restrictive mode before anything is written to
        # it, rather than chmod-ing after the first write leaves a window where
        # a filename list is readable at the umask default.
        try:
            os.chmod(db, 0o600)
        except OSError:
            pass

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SystemExit(
            f"cdm: index was written by a newer version (schema {version}, "
            f"this build understands {SCHEMA_VERSION}). Upgrade cdm."
        )
    conn.executescript(SCHEMA)
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
