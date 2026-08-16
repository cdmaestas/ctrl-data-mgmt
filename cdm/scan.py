"""The crawler: N walker threads, one writer thread, resumable.

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

CONCURRENCY

Walking is latency-bound, not CPU-bound, and CPython releases the GIL around
scandir and stat -- so threads work here despite the usual advice. Measured
against simulated metadata latency: ~27x at 32 threads for a 0.5ms round trip,
but a 4x LOSS on a warm local disk where there is no latency to hide. See
probe.py for how the thread count gets chosen.

Exactly one thread touches SQLite. Walkers hand (directory, rows) to a bounded
queue; the writer drains it. The bound is what keeps memory flat on a tree with
a hundred million entries -- when the writer falls behind, walkers block instead
of buffering the filesystem into RAM.

HASHING stays single-threaded on purpose. It is bandwidth-bound rather than
latency-bound, so concurrency buys little, and saturating a shared filesystem's
I/O is the kind of thing that ends an engagement.
"""
from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import hashing
from .exclude import Excluder

PROGRESS_EVERY = 5000
QUEUE_DEPTH = 64
COMMIT_EVERY_DIRS = 200
COMMIT_EVERY_ROWS = 5000

_INSERT = (
    "INSERT INTO files (host, root, path, parent, name, size, mtime, ctime, "
    "                   inode, type, hash, hash_kind, hash_size, hash_mtime, seen_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(host, path) DO UPDATE SET "
    "  root=excluded.root, parent=excluded.parent, name=excluded.name, "
    "  size=excluded.size, mtime=excluded.mtime, ctime=excluded.ctime, "
    "  inode=excluded.inode, type=excluded.type, hash=excluded.hash, "
    "  hash_kind=excluded.hash_kind, hash_size=excluded.hash_size, "
    "  hash_mtime=excluded.hash_mtime, seen_at=excluded.seen_at"
)


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
    threads: int = 1
    resumed_from: int = 0

    @property
    def total(self) -> int:
        return self.files + self.dirs + self.links


def _open_scan(conn, host, root_key, hash_kind, resume):
    """Find a resumable scan or start a new one.

    A scan is only resumable if it asked for the same hash kind. Resuming a
    stat-only scan with --checksum would leave half the tree hashed and half
    not, with nothing recording which half.
    """
    if resume:
        row = conn.execute(
            "SELECT scan_id, started_at, hash_kind FROM scans "
            "WHERE host = ? AND root = ? AND finished_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (host, root_key),
        ).fetchone()
        if row is not None and row["hash_kind"] == hash_kind:
            done = {
                r[0] for r in conn.execute(
                    "SELECT path FROM scan_dirs WHERE host = ? AND root = ? "
                    "AND scan_id = ?", (host, root_key, row["scan_id"]))
            }
            return row["scan_id"], row["started_at"], done

    scan_id = uuid.uuid4().hex[:12]
    stamp = datetime.now().isoformat(timespec="microseconds")
    conn.execute(
        "INSERT INTO scans (host, root, scan_id, started_at, hash_kind) "
        "VALUES (?,?,?,?,?)", (host, root_key, scan_id, stamp, hash_kind))
    conn.commit()
    return scan_id, stamp, set()


def _resumed_children(conn, host, root_key, stamp, completed):
    """Map each completed directory to its subdirectories, from the index.

    On resume the index already knows what a finished directory contained, so
    the walk descends past it without re-reading the disk.

    One query, not one per directory. The per-directory version made resuming a
    4,600-directory checkpoint take 19.9s against 2.5s for simply starting over
    -- a resume slower than a restart is worse than no resume at all.
    """
    children: dict[Path, list[Path]] = {}
    if not completed:
        return children
    for parent, path in conn.execute(
        "SELECT parent, path FROM files WHERE host = ? AND root = ? "
        "AND type = 'dir' AND seen_at = ?", (host, root_key, stamp)
    ):
        parent_path = Path(parent)
        if parent in completed:
            children.setdefault(parent_path, []).append(Path(path))
    # A completed directory with no subdirectories still needs an entry, or the
    # walker treats it as unvisited and re-reads it.
    for done in completed:
        children.setdefault(Path(done), [])
    return children


def scan_root(conn, host: str, root: Path, *, hash_kind: str | None = None,
              max_hash_bytes: int | None = None, excluder: Excluder | None = None,
              now: str | None = None, progress=None, threads: int = 1,
              resume: bool = True) -> ScanStats:
    """Index everything under `root`, updating rows in place.

    `hash_kind` is None (stat only), 'partial' or 'full'.
    `threads` is the number of walker threads; the writer is always separate.
    `resume` picks up an unfinished scan of the same root and hash kind.
    """
    started = time.time()
    root = Path(root).expanduser().resolve()
    root_key = str(root)
    if not root.is_dir():
        raise NotADirectoryError(root_key)

    ex = excluder or Excluder()
    stats = ScanStats(threads=threads)
    threads = max(1, threads)

    scan_id, stamp, completed = _open_scan(conn, host, root_key, hash_kind, resume)
    if now is not None:
        stamp = now
    stats.resumed_from = len(completed)

    known = {
        r["path"]: r for r in conn.execute(
            "SELECT path, size, mtime, hash, hash_kind, hash_size, hash_mtime "
            "FROM files WHERE host = ? AND root = ?", (host, root_key))
    }

    # Pre-computed in the main thread: walkers must never touch the connection.
    resumed_children = _resumed_children(conn, host, root_key, stamp, completed)

    work: queue.Queue = queue.Queue()
    out: queue.Queue = queue.Queue(maxsize=QUEUE_DEPTH)
    lock = threading.Lock()
    seen_dirs: set[tuple[int, int]] = set()
    inflight = [0]
    failed: list[BaseException] = []

    def submit(directory: Path) -> None:
        with lock:
            inflight[0] += 1
        work.put(directory)

    def finish_one() -> None:
        with lock:
            inflight[0] -= 1
            empty = inflight[0] == 0
        if empty:
            for _ in range(threads):
                work.put(None)

    def hash_for(path: Path, st, kind: str):
        """Returns (digest, kind, size, mtime) or Nones."""
        if kind != "file" or hash_kind is None:
            return None, None, None, None
        size = st.st_size
        prior = known.get(str(path))
        if (prior is not None and prior["hash"] is not None
                and prior["hash_kind"] == hash_kind
                and prior["hash_size"] == size
                and prior["hash_mtime"] == st.st_mtime):
            with lock:
                stats.reused_hashes += 1
            return (prior["hash"], prior["hash_kind"], prior["hash_size"],
                    prior["hash_mtime"])
        if max_hash_bytes is not None and size > max_hash_bytes:
            return None, None, None, None
        try:
            digest = hashing.compute(path, size, hash_kind)
        except OSError:
            with lock:
                stats.unreadable.append(str(path))
            return None, None, None, None
        with lock:
            stats.hashed += 1
        return digest, hash_kind, size, st.st_mtime

    def walk_one(directory: Path):
        """Enumerate one directory. Returns (rows, subdirectories)."""
        rows, subdirs = [], []
        local = {"files": 0, "dirs": 0, "links": 0}
        try:
            entries = list(os.scandir(directory))
        except OSError:
            with lock:
                stats.unreadable.append(str(directory))
            return rows, subdirs

        for entry in entries:
            path = Path(entry.path)
            with lock:
                if ex.excludes(path):
                    continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                with lock:
                    stats.unreadable.append(str(path))
                continue

            if entry.is_symlink():
                kind = "link"
                local["links"] += 1
            elif entry.is_dir(follow_symlinks=False):
                key = (st.st_dev, st.st_ino)
                with lock:
                    if key in seen_dirs:
                        continue
                    seen_dirs.add(key)
                kind = "dir"
                local["dirs"] += 1
                subdirs.append(path)
            else:
                kind = "file"
                local["files"] += 1

            digest, dkind, dsize, dmtime = hash_for(path, st, kind)
            rows.append((host, root_key, str(path), str(path.parent), path.name,
                         st.st_size, st.st_mtime, st.st_ctime, st.st_ino, kind,
                         digest, dkind, dsize, dmtime, stamp))

        with lock:
            stats.files += local["files"]
            stats.dirs += local["dirs"]
            stats.links += local["links"]
        return rows, subdirs

    def walker() -> None:
        while True:
            directory = work.get()
            if directory is None:
                return
            try:
                if directory in resumed_children:
                    # Already recorded by an earlier run: descend, don't re-read.
                    for child in resumed_children[directory]:
                        submit(child)
                else:
                    rows, subdirs = walk_one(directory)
                    out.put((str(directory), rows))
                    for child in subdirs:
                        submit(child)
            except BaseException as exc:  # noqa: BLE001 - must not deadlock
                with lock:
                    failed.append(exc)
            finally:
                finish_one()

    # The main thread is the writer. SQLite connections cannot be shared across
    # threads, and the alternatives -- check_same_thread=False, or a second
    # connection to the same file -- both add risk to buy nothing: this thread
    # has no other work while the walkers run.
    walkers = [threading.Thread(target=walker, name=f"cdm-walk-{i}", daemon=True)
               for i in range(threads)]
    for t in walkers:
        t.start()

    submit(root)

    # A reaper closes the output queue once every walker has exited, so the
    # writer loop below can block on get() instead of polling. Polling cost a
    # tenth of a second per scan, which is invisible on a real tree and
    # dominates a test suite of small ones.
    def reap() -> None:
        for t in walkers:
            t.join()
        out.put(None)

    reaper = threading.Thread(target=reap, name="cdm-reap", daemon=True)
    reaper.start()

    pending_rows = pending_dirs = 0
    last_progress = 0
    while True:
        item = out.get()
        if item is None:
            break
        directory, rows = item

        if rows:
            conn.executemany(_INSERT, rows)
        # Written in the SAME transaction as the rows it covers. See db.py.
        conn.execute(
            "INSERT OR IGNORE INTO scan_dirs (host, root, scan_id, path) "
            "VALUES (?,?,?,?)", (host, root_key, scan_id, directory))
        pending_rows += len(rows)
        pending_dirs += 1
        if pending_rows >= COMMIT_EVERY_ROWS or pending_dirs >= COMMIT_EVERY_DIRS:
            conn.commit()
            pending_rows = pending_dirs = 0
        if progress is not None and stats.total - last_progress >= PROGRESS_EVERY:
            last_progress = stats.total
            progress(stats)

    reaper.join()
    conn.commit()

    if failed:
        raise failed[0]

    # Anything under this root that this pass did not touch is gone from disk.
    cur = conn.execute(
        "DELETE FROM files WHERE host = ? AND root = ? AND seen_at < ?",
        (host, root_key, stamp))
    stats.pruned = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    conn.execute(
        "UPDATE scans SET finished_at = ? WHERE host = ? AND root = ? AND scan_id = ?",
        (datetime.now().isoformat(timespec="microseconds"), host, root_key, scan_id))
    # The checkpoint has served its purpose; keeping it would grow the index by
    # one row per directory per scan, forever.
    conn.execute("DELETE FROM scan_dirs WHERE host = ? AND root = ? AND scan_id = ?",
                 (host, root_key, scan_id))
    conn.execute(
        "INSERT INTO roots (host, path, added_at, last_scan) VALUES (?,?,?,?) "
        "ON CONFLICT(host, path) DO UPDATE SET last_scan=excluded.last_scan",
        (host, root_key, stamp, stamp))
    conn.commit()

    stats.elapsed = time.time() - started
    return stats
