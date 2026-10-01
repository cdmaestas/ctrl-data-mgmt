"""`cdm import --policy`: load a Storage Scale policy listing into the index.

The listing comes from the script `cdm policy` prints (see policy.py). Rows are
recorded under a logical host, `<filesystem>@<cluster>`, so listings taken from
any node of a cluster line up, and the root is marked `source = 'policy'` so
every question sees it alongside this machine's own scans (roots.py).

TWO PASSES, on purpose. The first reads the whole listing and proves it is
complete -- header, every line well formed, end marker present and matching
the row count -- before anything is written. A truncated or damaged listing is
refused and the index is not touched. Neither pass holds the listing in
memory, so a listing of tens of millions of lines costs two sequential reads.

AN IMPORT IS A SNAPSHOT of its scope. Rows the listing no longer contains are
removed -- which is only safe because pass one already proved the listing
complete. The scope is what the listing actually covers, not just a path:
a fileset listing (FOR FILESET) leaves out other filesets even when they are
linked under its junction, so re-importing fileset `proj` removes only
`proj`'s missing rows, never a nested fileset's. A whole-filesystem listing
covers everything under its root. Rows an older import stored without a
fileset are removed only if this root owns them; nothing is guessed.

A re-import keeps a file's hash when its size and mtime are unchanged, the same
rule a rescan uses. Not yet, each its own piece of Phase 2: access times and
"never opened" (#10), fileset and pool in find and MCP (#11), hashing (#12).
"""
from __future__ import annotations

import posixpath
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import policy
from . import roots as roots_mod

BATCH = 5000
PROGRESS_EVERY = 50_000
PROGRESS_SECONDS = 1.0

# Keeps an existing hash only while the file it was computed against is
# unchanged (same rule as scan.hash_for); anything else invalidates it.
_UPSERT = (
    "INSERT INTO files (host, root, path, parent, name, size, mtime, ctime, inode, "
    "                   type, seen_at, fileset, pool) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(host, path) DO UPDATE SET "
    "  root=excluded.root, parent=excluded.parent, name=excluded.name, "
    "  size=excluded.size, mtime=excluded.mtime, ctime=excluded.ctime, "
    "  inode=excluded.inode, type=excluded.type, seen_at=excluded.seen_at, "
    "  fileset=excluded.fileset, pool=excluded.pool, "
    "  hash=CASE WHEN files.hash_size=excluded.size AND files.hash_mtime=excluded.mtime "
    "            THEN files.hash END, "
    "  hash_kind=CASE WHEN files.hash_size=excluded.size "
    "                  AND files.hash_mtime=excluded.mtime THEN files.hash_kind END, "
    "  hash_size=CASE WHEN files.hash_size=excluded.size "
    "                  AND files.hash_mtime=excluded.mtime THEN files.hash_size END, "
    "  hash_mtime=CASE WHEN files.hash_size=excluded.size "
    "                   AND files.hash_mtime=excluded.mtime THEN files.hash_mtime END"
)


@dataclass
class ImportStats:
    host: str = ""
    root: str = ""
    files: int = 0
    dirs: int = 0
    links: int = 0
    pruned: int = 0          # rows the listing no longer contains
    hashed: int = 0          # the progress line shows hashing when there is any
    hashed_bytes: int = 0
    started: float = 0.0
    elapsed: float = 0.0

    @property
    def total(self) -> int:
        return self.files + self.dirs + self.links

    @property
    def running_for(self) -> float:
        return self.elapsed or (time.time() - self.started if self.started else 0.0)

    def rates(self) -> tuple[float, float]:
        secs = self.running_for
        return (self.total / secs if secs > 0 else 0.0), 0.0


def _open(path: Path):
    # Paths in a listing are percent-encoded ASCII; the header is ASCII. Any
    # stray byte is kept, not guessed at, and rejected by the parser if wrong.
    return path.open(encoding="utf-8", errors="surrogateescape")


def _survey(path: Path) -> tuple[dict[str, str], int, str | None]:
    """Pass one: (header, entries, common directory). Raises ListingError."""
    with _open(path) as f:
        header, entries, state = policy.read_listing(f)
        count, common, only_kind = 0, None, None
        for e in entries:
            count += 1
            common = e.path if common is None else posixpath.commonpath([common, e.path])
            only_kind = e.kind if count == 1 else None
    if not state["complete"]:
        raise policy.ListingError(
            f"{path} has no end marker, so it is incomplete (a run cut short, or "
            f"a partial copy); nothing was imported")
    if common is None:
        return header, 0, None
    if only_kind in ("file", "link"):
        # A listing of one file: its directory is the root. (A common path can
        # only be a file when the file is the only entry.)
        common = posixpath.dirname(common)
    return header, count, common


def _prune(conn, host: str, root_key: str, scope: str, stamp: str) -> int:
    """Remove rows under `root_key` that this complete listing no longer has.

    Only rows the listing would have contained: for `fileset X`, rows of
    fileset X (and rows from an older import with no fileset recorded, if this
    root owns them); for `filesystem`, everything under the root. The root's
    own row is not touched -- a root records its contents, not itself.
    """
    lo, hi = roots_mod.bounds(root_key)
    where = "host = ? AND path >= ? AND path < ? AND seen_at != ?"
    params: list = [host, lo, hi, stamp]
    if scope.startswith("fileset "):
        where += " AND (fileset = ? OR (fileset IS NULL AND root = ?))"
        params += [scope[len("fileset "):], root_key]
    return max(conn.execute(f"DELETE FROM files WHERE {where}", params).rowcount, 0)


def import_policy(conn, path: Path | str, *, root: str | None = None,
                  progress=None) -> ImportStats:
    """Import one complete listing. Raises ListingError, having changed nothing,
    if the listing is incomplete or malformed, or an entry lies outside `root`."""
    path = Path(path)
    stats = ImportStats(started=time.time())
    header, _count, common = _survey(path)
    scope = header["scope"]
    if scope != "filesystem" and not scope.startswith("fileset "):
        # Checked before writing: the scope decides what a re-import may remove.
        raise policy.ListingError(f"unknown scope {scope!r}; nothing was imported")
    if root is None and common is None:
        # Complete and empty is a real snapshot -- everything is gone -- but
        # with no paths there is nothing to infer the root from.
        raise policy.ListingError(
            f"{path} is complete but lists nothing; pass --root to say which root "
            f"it covers (everything under it will be removed); nothing was imported")
    root_key = posixpath.normpath(root) if root else common
    if not root_key.startswith("/"):
        raise policy.ListingError(f"root {root_key!r} must be an absolute path")
    host = f"{header['device']}@{header['cluster']}"
    stats.host, stats.root = host, root_key
    stamp = datetime.now().isoformat(timespec="microseconds")
    inner = roots_mod.nested(conn, host, root_key)
    below = roots_mod.bounds(root_key)[0]

    batch: list[tuple] = []
    last_progress, last_at = 0, time.time()
    with _open(path) as f:
        _, entries, _state = policy.read_listing(f)
        for e in entries:
            if e.path == root_key:
                # A root records what is in it, not itself, as a scan does.
                continue
            if not e.path.startswith(below):
                conn.rollback()
                raise policy.ListingError(
                    f"{e.path} is outside the root {root_key}; nothing was imported")
            parent, name = posixpath.split(e.path)
            owner = roots_mod.owner(parent, root_key, inner)
            batch.append((host, owner, e.path, parent, name, e.size, e.mtime, e.ctime,
                          e.inode, e.kind, stamp, e.fileset, e.pool))
            if e.kind == "dir":
                stats.dirs += 1
            elif e.kind == "link":
                stats.links += 1
            else:
                stats.files += 1
            if len(batch) >= BATCH:
                conn.executemany(_UPSERT, batch)
                batch.clear()
            if progress is not None and (
                    stats.total - last_progress >= PROGRESS_EVERY
                    or time.time() - last_at >= PROGRESS_SECONDS):
                last_progress, last_at = stats.total, time.time()
                progress(stats)
    if batch:
        conn.executemany(_UPSERT, batch)
    stats.pruned = _prune(conn, host, root_key, scope, stamp)
    conn.execute(
        "INSERT INTO roots (host, path, added_at, last_scan, source) VALUES (?,?,?,?,?) "
        "ON CONFLICT(host, path) DO UPDATE SET last_scan=excluded.last_scan, "
        "source=excluded.source",
        (host, root_key, stamp, stamp, roots_mod.POLICY))
    # One transaction: an import interrupted part way leaves the index as it
    # was, not half-updated.
    conn.commit()
    stats.elapsed = time.time() - stats.started
    return stats
