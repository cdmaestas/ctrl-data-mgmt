"""The crawler.

Rules that are not negotiable, because getting them wrong is how a scan becomes
a hang or a lie:

* Roots are explicit. There is no default $HOME crawl -- a tool that indexes
  your whole home directory the first time you run it is a tool people uninstall.
* Symlinks are never followed. One link into /proc, or one cycle, turns a scan
  into an infinite walk. Links are recorded as rows of type 'link'; what they
  point at is somebody else's root.
* Devices and inodes seen already are not revisited, which catches bind mounts
  and hardlinked directory trees.
* Rehashing is skipped when size and mtime are unchanged, so a rescan of a
  quiet tree costs one stat per file.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import hashing
from .exclude import Excluder


@dataclass
class ScanStats:
    files: int = 0
    dirs: int = 0
    links: int = 0
    hashed: int = 0
    reused_hashes: int = 0
    pruned: int = 0
    unreadable: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def total(self) -> int:
        return self.files + self.dirs + self.links


def _existing_rows(conn, host: str, root: str) -> dict:
    rows = conn.execute(
        "SELECT path, size, mtime, hash, hash_kind, hash_size, hash_mtime "
        "FROM files WHERE host = ? AND root = ?",
        (host, root),
    )
    return {r["path"]: r for r in rows}


def scan_root(conn, host: str, root: Path, *, hash_kind: str | None = None,
              max_hash_bytes: int | None = None, excluder: Excluder | None = None,
              now: str | None = None) -> ScanStats:
    """Index everything under `root`, updating rows in place.

    `hash_kind` is None (stat only), 'partial' or 'full'.
    """
    started = time.time()
    root = Path(root).expanduser().resolve()
    root_key = str(root)
    # Microsecond resolution, because the prune step compares seen_at
    # lexicographically: two scans inside the same second would leave deleted
    # files in the index.
    stamp = now or datetime.now().isoformat(timespec="microseconds")
    ex = excluder or Excluder()
    stats = ScanStats()

    known = _existing_rows(conn, host, root_key)
    seen_dirs: set[tuple[int, int]] = set()
    batch: list[tuple] = []

    def flush() -> None:
        if not batch:
            return
        conn.executemany(
            "INSERT INTO files (host, root, path, parent, name, size, mtime, ctime, "
            "                   inode, type, hash, hash_kind, hash_size, hash_mtime, seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(host, path) DO UPDATE SET "
            "  root=excluded.root, parent=excluded.parent, name=excluded.name, "
            "  size=excluded.size, mtime=excluded.mtime, ctime=excluded.ctime, "
            "  inode=excluded.inode, type=excluded.type, hash=excluded.hash, "
            "  hash_kind=excluded.hash_kind, hash_size=excluded.hash_size, "
            "  hash_mtime=excluded.hash_mtime, seen_at=excluded.seen_at",
            batch,
        )
        batch.clear()

    def record(path: Path, st: os.stat_result, kind: str) -> None:
        size = st.st_size
        digest = digest_kind = digest_size = digest_mtime = None

        if kind == "file" and hash_kind is not None:
            prior = known.get(str(path))
            fresh = (
                prior is not None
                and prior["hash"] is not None
                and prior["hash_kind"] == hash_kind
                and prior["hash_size"] == size
                and prior["hash_mtime"] == st.st_mtime
            )
            if fresh:
                digest = prior["hash"]
                digest_kind = prior["hash_kind"]
                digest_size = prior["hash_size"]
                digest_mtime = prior["hash_mtime"]
                stats.reused_hashes += 1
            elif max_hash_bytes is not None and size > max_hash_bytes:
                pass  # over the cap: recorded, just not hashed
            else:
                try:
                    digest = hashing.compute(path, size, hash_kind)
                    digest_kind, digest_size, digest_mtime = hash_kind, size, st.st_mtime
                    stats.hashed += 1
                except OSError:
                    stats.unreadable.append(str(path))

        batch.append((
            host, root_key, str(path), str(path.parent), path.name,
            size, st.st_mtime, st.st_ctime, st.st_ino, kind,
            digest, digest_kind, digest_size, digest_mtime, stamp,
        ))
        if len(batch) >= 500:
            flush()

    def walk(start: Path) -> None:
        # An explicit stack, not recursion: a tree deeper than the interpreter's
        # recursion limit is unusual but entirely legal, and a crawler that
        # crashes on one is no use.
        pending = [start]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                stats.unreadable.append(str(directory))
                continue

            for entry in entries:
                path = Path(entry.path)
                if ex.excludes(path):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    stats.unreadable.append(str(path))
                    continue

                if entry.is_symlink():
                    stats.links += 1
                    record(path, st, "link")
                elif entry.is_dir(follow_symlinks=False):
                    key = (st.st_dev, st.st_ino)
                    if key in seen_dirs:
                        continue
                    seen_dirs.add(key)
                    stats.dirs += 1
                    record(path, st, "dir")
                    pending.append(path)
                else:
                    stats.files += 1
                    record(path, st, "file")

    if not root.is_dir():
        raise NotADirectoryError(root_key)

    walk(root)
    flush()

    # Anything under this root that this pass did not touch is gone from disk.
    cur = conn.execute(
        "DELETE FROM files WHERE host = ? AND root = ? AND seen_at < ?",
        (host, root_key, stamp),
    )
    stats.pruned = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    conn.execute(
        "INSERT INTO roots (host, path, added_at, last_scan) VALUES (?,?,?,?) "
        "ON CONFLICT(host, path) DO UPDATE SET last_scan=excluded.last_scan",
        (host, root_key, stamp, stamp),
    )
    conn.commit()

    stats.elapsed = time.time() - started
    return stats
