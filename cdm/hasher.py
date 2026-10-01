"""`cdm hash`: hash the files that could be duplicates, and nothing else.

A scan hashes as it walks (`--checksum`). An imported Storage Scale listing
brings no contents, so `cdm hash` reads them afterwards -- on a node that
mounts the filesystem, at the paths the listing recorded.

ONLY SIZE-MATCHED FILES. Two files can only be duplicates if they are the same
size, so a file whose size no other indexed file shares is never read. On a
large filesystem that is most files. Sizes are matched across everything this
index covers (this machine's scans and every imported listing), so a cluster
file can be found to duplicate one on a workstation.

RULES, the same as a scan's:
* A valid hash of the requested kind is kept; only missing or stale ones are
  computed. A rerun therefore resumes where the last one stopped.
* A file whose size or mtime no longer matches its row is not hashed: the
  hash would describe content the index has not seen. Re-import or rescan.
* A file that is not on this machine is counted, not an error: the index may
  describe another node's disks.
* cdm's own reads must not look like use (ADR 0005). Hashing opens files with
  O_NOATIME where the OS allows; otherwise the atime its read left behind is
  recorded as self_atime, and the next scan or import keeps the earlier last
  read if nothing else has read the file since.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from . import hashing
from . import roots as roots_mod

PAGE = 2000
PROGRESS_SECONDS = 1.0


@dataclass
class HashStats:
    candidates: int = 0
    hashed: int = 0
    hashed_bytes: int = 0
    changed: int = 0          # size or mtime differ from the index: re-import first
    elsewhere: int = 0        # not on this machine
    unreadable: list[str] = field(default_factory=list)
    started: float = 0.0
    elapsed: float = 0.0

    @property
    def total(self) -> int:   # what the progress line counts
        return self.hashed + self.changed + self.elsewhere + len(self.unreadable)

    @property
    def running_for(self) -> float:
        return self.elapsed or (time.time() - self.started if self.started else 0.0)

    def rates(self) -> tuple[float, float]:
        secs = self.running_for
        if secs <= 0:
            return 0.0, 0.0
        return self.total / secs, self.hashed_bytes / secs


def _scope(hosts, root: str | None, fileset: str | None, min_size: int):
    where, params = roots_mod.host_sql(hosts)
    where += " AND type = 'file' AND size >= ?"
    params = [*params, max(1, min_size)]
    if root is not None:
        lo, hi = roots_mod.bounds(root)
        where += " AND (path >= ? AND path < ?)"
        params += [lo, hi]
    if fileset is not None:
        where += " AND fileset = ?"
        params.append(fileset)
    return where, params


# Missing, of another kind, or computed against a different size or mtime.
# Takes the wanted kind as its one parameter.
_NEEDS = ("(hash IS NULL OR hash_kind != ? OR hash_size IS NOT size "
          "OR hash_mtime IS NOT mtime)")


def candidates(conn, hosts, *, root=None, fileset=None, kind=hashing.PARTIAL,
               min_size: int = 1) -> int:
    """How many files in scope share a size with another file and lack a hash."""
    all_where, all_params = _scope(hosts, None, None, min_size)
    where, params = _scope(hosts, root, fileset, min_size)
    return conn.execute(
        f"SELECT COUNT(*) FROM files WHERE {where} AND {_NEEDS} AND size IN "
        f"(SELECT size FROM files WHERE {all_where} GROUP BY size HAVING COUNT(*) > 1)",
        [*params, kind, *all_params]).fetchone()[0]


def hash_files(conn, hosts, *, root=None, fileset=None, kind=hashing.PARTIAL,
               min_size: int = 1, progress=None) -> HashStats:
    """Hash every size-matched, unhashed file in scope that is on this machine."""
    stats = HashStats(started=time.time())
    stats.candidates = candidates(conn, hosts, root=root, fileset=fileset, kind=kind,
                                  min_size=min_size)
    all_where, all_params = _scope(hosts, None, None, min_size)
    where, params = _scope(hosts, root, fileset, min_size)
    sql = (f"SELECT host, path, size, mtime, atime FROM files WHERE {where} "
           f"AND {_NEEDS} AND size IN (SELECT size FROM files WHERE {all_where} "
           f"GROUP BY size HAVING COUNT(*) > 1) AND path > ? ORDER BY path LIMIT ?")
    after, last_progress = "", time.time()
    while True:
        # Keyset pages, so millions of candidates never sit in memory and the
        # rows being updated are never the ones being read.
        page = conn.execute(sql, [*params, kind, *all_params, after, PAGE]).fetchall()
        if not page:
            break
        for r in page:
            _one(conn, r, kind, stats)
            if progress is not None and time.time() - last_progress >= PROGRESS_SECONDS:
                last_progress = time.time()
                progress(stats)
        after = page[-1]["path"]
        conn.commit()           # each page is durable: a rerun resumes after it
    stats.elapsed = time.time() - stats.started
    return stats


def _one(conn, r, kind: str, stats: HashStats) -> None:
    path = r["path"]
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        stats.elsewhere += 1
        return
    except OSError:
        stats.unreadable.append(path)
        return
    # The index describes a different version of this file. Imported times are
    # microseconds and stat's are finer, so allow for that before calling it
    # changed.
    if st.st_size != r["size"] or abs(st.st_mtime - r["mtime"]) >= 1e-6:
        stats.changed += 1
        return
    try:
        digest = hashing.compute(path, r["size"], kind)
    except OSError:
        stats.unreadable.append(path)
        return
    own = None
    if r["atime"] is not None:
        try:
            own = os.stat(path).st_atime
        except OSError:
            # Cannot see what the read did, so cannot discount it: the access
            # time becomes unknown rather than mistaken for use (as scan.py).
            conn.execute("UPDATE files SET atime = NULL, unopened_until = NULL "
                         "WHERE host = ? AND path = ?", (r["host"], path))
    conn.execute(
        "UPDATE files SET hash = ?, hash_kind = ?, hash_size = ?, hash_mtime = ?, "
        "self_atime = COALESCE(?, self_atime) WHERE host = ? AND path = ?",
        (digest, kind, r["size"], r["mtime"], own, r["host"], path))
    stats.hashed += 1
    stats.hashed_bytes += hashing.bytes_read(r["size"], kind)
