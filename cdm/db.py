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

SCHEMA_VERSION = 2

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
-- Resume reads back the directory tree by parent; without this it is a table
-- scan per lookup and resuming costs more than rescanning from scratch.
CREATE INDEX IF NOT EXISTS idx_files_parent ON files(host, root, type, seen_at);

-- Checkpointing, so an interrupted scan can resume instead of restarting.
--
-- A row in scan_dirs means "this directory's entries are committed". It is
-- written in the SAME transaction as those entries, which is the whole point:
-- commit them separately and a crash between the two leaves a directory
-- marked done whose files were never recorded, and the resumed scan skips it
-- forever.
CREATE TABLE IF NOT EXISTS scans (
    host        TEXT NOT NULL,
    root        TEXT NOT NULL,
    scan_id     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    hash_kind   TEXT,
    PRIMARY KEY (host, root, scan_id)
);

CREATE TABLE IF NOT EXISTS scan_dirs (
    host    TEXT NOT NULL,
    root    TEXT NOT NULL,
    scan_id TEXT NOT NULL,
    path    TEXT NOT NULL,
    PRIMARY KEY (host, root, scan_id, path)
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the index, at 0600."""
    db = Path(path) if path else paths.index_path()
    if db.parent != Path("."):
        db.parent.mkdir(parents=True, exist_ok=True)
        paths.warn(paths.enforce_mode(db.parent, paths.DIR_MODE))

    # Create the file ourselves, at 0600, instead of letting sqlite3 create it
    # at the umask default. With umask 022 that default is 0644, and the window
    # between creation and a later chmod is a window where every user on the
    # machine can read the index.
    if not db.exists():
        os.close(os.open(str(db), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    paths.warn(paths.enforce_mode(db, paths.FILE_MODE))

    # ORDER MATTERS AND IS NOT COSMETIC. SQLite copies the main database file's
    # permissions onto -wal and -shm when it creates them, so the mode above
    # must be correct BEFORE journal_mode=WAL runs. Enable WAL first and those
    # two files are created 0644 under a default umask -- holding the same
    # filename data the index exists to protect. Verified by test.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    for sidecar in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        if sidecar.exists():
            paths.warn(paths.enforce_mode(sidecar, paths.FILE_MODE))
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
