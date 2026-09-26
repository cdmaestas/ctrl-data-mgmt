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
* The index's own files are never opened. Closing ANY descriptor to a file
  drops every POSIX lock this process holds on it, SQLite's included; hashing
  index.db-shm mid-scan let a concurrent reader decide it was the last
  connection and delete -wal and -shm out from under the writer. Observed: a
  SIGBUS in walFindFrame scanning $HOME while another process read the index.

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
from . import roots as roots_mod
from .exclude import Excluder

PROGRESS_EVERY = 5000
# ...or this often, whichever comes first, so a slow filesystem still reports.
PROGRESS_SECONDS = 1.0
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
    hashed_bytes: int = 0
    reused_hashes: int = 0
    pruned: int = 0
    unreadable: list[str] = field(default_factory=list)
    started: float = 0.0
    elapsed: float = 0.0
    threads: int = 1
    resumed_from: int = 0
    root_unreadable: bool = False

    @property
    def total(self) -> int:
        return self.files + self.dirs + self.links

    @property
    def running_for(self) -> float:
        """Seconds so far: final once the scan returns, live while it runs."""
        if self.elapsed:
            return self.elapsed
        return time.time() - self.started if self.started else 0.0

    def rates(self) -> tuple[float, float]:
        """(entries per second, hashed bytes per second) for THIS run.

        A resumed scan only counts what it did itself, so these are honest
        throughput figures rather than inflated by checkpointed work.
        """
        secs = self.running_for
        if secs <= 0:
            return 0.0, 0.0
        return self.total / secs, self.hashed_bytes / secs


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


def _index_files(conn) -> frozenset[str]:
    """Paths of the database behind `conn` and its sidecars, resolved.

    Empty for an in-memory or temporary database, which has nothing on disk
    for the walk to stumble into.
    """
    names = set()
    for row in conn.execute("PRAGMA database_list"):
        main = row[2]
        if main:
            base = str(Path(main).resolve())
            names.update(base + suffix for suffix in ("", "-wal", "-shm", "-journal"))
    return frozenset(names)


def _resumed_children(conn, host, owners, stamp, completed):
    """Map each completed directory to its subdirectories, from the index.

    On resume the index already knows what a finished directory contained, so
    the walk descends past it without re-reading the disk.

    One query, not one per directory. The per-directory version made resuming a
    4,600-directory checkpoint take 19.9s against 2.5s for simply starting over
    -- a resume slower than a restart is worse than no resume at all.

    `owners` is every root this scan writes rows for: its own and the roots
    nested inside it. Looking only at its own would find no children for a
    completed directory inside a nested root, and the walk would silently stop
    there. `seen_at >= stamp`, not `=`, for the same reason: a scan of a nested
    root that ran since the checkpoint rewrote those rows with a later stamp.
    """
    children: dict[Path, list[Path]] = {}
    if not completed:
        return children
    marks = ",".join("?" * len(owners))
    for parent, path in conn.execute(
        f"SELECT parent, path FROM files WHERE host = ? AND root IN ({marks}) "
        f"AND type = 'dir' AND seen_at >= ?", (host, *owners, stamp)
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
    stats = ScanStats(threads=threads, started=started)
    threads = max(1, threads)
    own_files = _index_files(conn)

    scan_id, stamp, completed = _open_scan(conn, host, root_key, hash_kind, resume)
    if now is not None:
        stamp = now
    stats.resumed_from = len(completed)

    # Rows below this root are owned by the most specific registered root (see
    # roots.py), so hashes to reuse are found by path, never by owner.
    lo, hi = roots_mod.bounds(root_key)
    known = {
        r["path"]: r for r in conn.execute(
            "SELECT path, size, mtime, hash, hash_kind, hash_size, hash_mtime "
            "FROM files WHERE host = ? AND path >= ? AND path < ?", (host, lo, hi))
    }
    inner = roots_mod.nested(conn, host, root_key)
    owners = [root_key, *inner]

    # Pre-computed in the main thread: walkers must never touch the connection.
    resumed_children = _resumed_children(conn, host, owners, stamp, completed)

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
        # Indexed, but never opened: see "The index's own files" above.
        if str(path) in own_files:
            return None, None, None, None
        try:
            digest = hashing.compute(path, size, hash_kind)
        except OSError:
            with lock:
                stats.unreadable.append(str(path))
            return None, None, None, None
        with lock:
            stats.hashed += 1
            stats.hashed_bytes += hashing.bytes_read(size, hash_kind)
        return digest, hash_kind, size, st.st_mtime

    def walk_one(directory: Path):
        """Enumerate one directory. Returns (rows, subdirectories, readable).

        `readable` is False when the directory could not be opened at all. That
        distinction is load-bearing twice over: an unreadable directory must not
        be checkpointed (a resumed scan would skip it forever), and nothing
        under it may be pruned (we did not see its contents because we could not
        look, not because they are gone).
        """
        rows, subdirs = [], []
        local = {"files": 0, "dirs": 0, "links": 0}
        owner = roots_mod.owner(str(directory), root_key, inner)
        try:
            entries = list(os.scandir(directory))
        except OSError:
            with lock:
                stats.unreadable.append(str(directory))
            return rows, subdirs, False

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
            rows.append((host, owner, str(path), str(path.parent), path.name,
                         st.st_size, st.st_mtime, st.st_ctime, st.st_ino, kind,
                         digest, dkind, dsize, dmtime, stamp))

        with lock:
            stats.files += local["files"]
            stats.dirs += local["dirs"]
            stats.links += local["links"]
        return rows, subdirs, True

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
                    rows, subdirs, readable = walk_one(directory)
                    # Only a directory we actually read gets handed to the
                    # writer, because reaching the writer is what records it as
                    # done and licenses pruning inside it.
                    if readable:
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
    last_progress_at = time.time()
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
        if progress is not None and (
                stats.total - last_progress >= PROGRESS_EVERY
                or time.time() - last_progress_at >= PROGRESS_SECONDS):
            last_progress = stats.total
            last_progress_at = time.time()
            progress(stats)

    reaper.join()
    conn.commit()

    if failed:
        raise failed[0]

    # The root itself is only in scan_dirs if it could be opened. If it could
    # not, this scan saw nothing and must not be allowed to draw conclusions.
    stats.root_unreadable = conn.execute(
        "SELECT 1 FROM scan_dirs WHERE host = ? AND root = ? AND scan_id = ? "
        "AND path = ?", (host, root_key, scan_id, root_key)).fetchone() is None

    # Prune ONLY inside directories this scan actually enumerated.
    #
    # The obvious version -- delete everything under the root whose seen_at is
    # old -- silently destroys the index when a tree is temporarily unreadable.
    # An unmounted NFS or GPFS share, a revoked Full Disk Access on macOS, a
    # permissions change: the walk records nothing, every existing row looks
    # stale, and a rescan reports thousands of files as "no longer on disk"
    # while they sit there untouched. Measured on a 6-row tree: all 6 deleted,
    # exit status 0.
    #
    # scan_dirs holds exactly the directories that were successfully read, so
    # restricting the delete to their children means absence is only ever
    # inferred from a directory we could actually see into.
    #
    # Rows owned by a nested root are pruned too: this scan read those
    # directories just as thoroughly as the nested root's own scan would.
    marks = ",".join("?" * len(owners))
    cur = conn.execute(
        f"DELETE FROM files WHERE host = ? AND root IN ({marks}) AND seen_at < ? "
        f"AND parent IN (SELECT path FROM scan_dirs "
        f"               WHERE host = ? AND root = ? AND scan_id = ?)",
        (host, *owners, stamp, host, root_key, scan_id))
    stats.pruned = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    conn.execute(
        "UPDATE scans SET finished_at = ? WHERE host = ? AND root = ? AND scan_id = ?",
        (datetime.now().isoformat(timespec="microseconds"), host, root_key, scan_id))
    # The checkpoint has served its purpose; keeping it would grow the index by
    # one row per directory per scan, forever.
    conn.execute("DELETE FROM scan_dirs WHERE host = ? AND root = ? AND scan_id = ?",
                 (host, root_key, scan_id))
    if not stats.root_unreadable:
        conn.execute(
            "INSERT INTO roots (host, path, added_at, last_scan) VALUES (?,?,?,?) "
            "ON CONFLICT(host, path) DO UPDATE SET last_scan=excluded.last_scan",
            (host, root_key, stamp, stamp))
    # A root that could not be opened gets no last_scan stamp. Recording one
    # would make "I could not look" indistinguishable from "I looked and it was
    # empty", which is the difference between an honest index and a misleading
    # one. An already-known root keeps its previous timestamp.
    conn.commit()

    stats.elapsed = time.time() - started
    return stats
