"""Reading the index: find, dupes, stat.

The filter set here is the surface a future natural-language layer would compile
into, so it is deliberately regular and composable rather than clever. Every
filter is an optional AND-ed clause over one column.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import hashing, roots

_SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2,
               "g": 1024 ** 3, "gb": 1024 ** 3, "t": 1024 ** 4, "tb": 1024 ** 4}

_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([kmgt]?b?)\s*$", re.I)
_AGO_RE = re.compile(r"^\s*([0-9]+)\s*([smhdw])\s*$", re.I)
_AGO_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_size(text: str) -> int:
    """'100M' -> 104857600. Binary units, because that is what df reports."""
    m = _SIZE_RE.match(text)
    if not m:
        raise ValueError(f"cannot read {text!r} as a size (try 100M, 2.5G, 4096)")
    return int(float(m.group(1)) * _SIZE_UNITS[m.group(2).lower()])


def parse_when(text: str, *, now: float | None = None) -> float:
    """'7d' -> epoch seconds 7 days ago. Also accepts YYYY-MM-DD."""
    now = time.time() if now is None else now
    m = _AGO_RE.match(text)
    if m:
        return now - int(m.group(1)) * _AGO_SECONDS[m.group(2).lower()]
    try:
        return time.mktime(time.strptime(text.strip(), "%Y-%m-%d"))
    except ValueError as exc:
        raise ValueError(
            f"cannot read {text!r} as a time (try 7d, 24h, or 2026-08-01)"
        ) from exc


def find(conn, *, host=None, root=None, name=None, iname=None, kind=None,
         larger_than=None, smaller_than=None, modified_after=None,
         modified_before=None, accessed_after=None, accessed_before=None,
         order="size", limit=100):
    # Every filter is one optional AND-ed clause over one column. Keeping them
    # in a table rather than a run of ifs is what makes the set easy to extend
    # -- and easy for a future NL layer to enumerate.
    #
    # `name GLOB ?` rather than LIKE: GLOB is case-sensitive and takes shell
    # wildcards, which is what someone typing --name '*.csv' expects.
    # `iname` exists because macOS and Windows filesystems are case-insensitive
    # while SQL GLOB is not: on APFS, `--name '*.txt'` misses `Report.TXT` even
    # though the filesystem itself treats those names as interchangeable.
    # lower() rather than a collation, because GLOB ignores COLLATE.
    root_key = str(Path(root).expanduser().resolve()) if root else None
    specs = (
        ("host = ?", host),
        # Everything under the root, including rows owned by roots nested in it.
        (roots.scope_sql(), roots.scope_params(root_key) if root_key else None),
        ("name GLOB ?", name),
        ("lower(name) GLOB ?", iname.lower() if iname else None),
        ("type = ?", kind),
        ("size > ?", larger_than),
        ("size < ?", smaller_than),
        ("mtime > ?", modified_after),
        ("mtime < ?", modified_before),
        # A NULL atime -- untrusted filesystem, a directory, not yet recorded --
        # matches neither, so "not accessed since" is never guessed.
        ("atime > ?", accessed_after),
        ("atime < ?", accessed_before),
    )
    clauses, params = [], []
    for sql_fragment, value in specs:
        if value is not None:
            clauses.append(sql_fragment)
            params.extend(value if isinstance(value, list) else [value])

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    order_sql = {"size": "size DESC", "mtime": "mtime DESC",
                 "atime": "atime IS NULL, atime DESC",
                 "name": "name ASC", "path": "path ASC"}[order]
    sql = f"SELECT * FROM files{where} ORDER BY {order_sql} LIMIT ?"
    return conn.execute(sql, (*params, limit)).fetchall()


def dupe_groups(conn, *, host=None, min_size=1, limit=100):
    """Files sharing a hash of the same kind and an identical size.

    Grouping includes hash_kind so a partial digest can never be pooled with a
    full one. Zero-length files are excluded by default: they all match, and
    saying so is noise rather than a finding.
    """
    clauses = ["hash IS NOT NULL", "size >= ?"]
    params: list = [min_size]
    if host:
        clauses.append("host = ?")
        params.append(host)

    sql = (
        f"SELECT hash, hash_kind, size, COUNT(*) AS n "
        f"FROM files WHERE {' AND '.join(clauses)} "
        f"GROUP BY hash, hash_kind, size HAVING n > 1 "
        f"ORDER BY size * (n - 1) DESC LIMIT ?"
    )
    groups = conn.execute(sql, (*params, limit)).fetchall()

    out = []
    for g in groups:
        members = conn.execute(
            "SELECT * FROM files WHERE hash = ? AND hash_kind = ? AND size = ? "
            "ORDER BY path",
            (g["hash"], g["hash_kind"], g["size"]),
        ).fetchall()
        out.append({"hash": g["hash"], "hash_kind": g["hash_kind"],
                    "size": g["size"], "count": g["n"],
                    "reclaimable": g["size"] * (g["n"] - 1),
                    "members": members})
    return out


def verify_group(group, reads: dict[str, float] | None = None
                 ) -> tuple[list[list[str]], list[str]]:
    """Re-hash a partial-hash group in full and split it into true duplicates.

    A partial hash proposes; this confirms. Returns (confirmed groups,
    unreadable paths) -- one list of paths per set of genuinely identical files
    (singletons dropped, they were false matches), plus everything that could
    not be read.

    The unreadable list is returned rather than skipped because silently
    dropping a file turns "these two are duplicates" into "these two are the
    ones I happened to be able to open", and the caller cannot tell the
    difference. On a shared filesystem, unreadable files are normal.
    """
    by_digest: dict[str, list[str]] = {}
    unreadable: list[str] = []
    for row in group["members"]:
        path = Path(row["path"])
        try:
            digest = hashing.full_hash(path)
        except OSError:
            unreadable.append(str(path))
            continue
        if reads is not None:
            # This read may have moved the atime; record_own_reads() stops it
            # being mistaken for use on the next scan.
            try:
                reads[str(path)] = os.stat(path).st_atime
            except OSError:
                pass
        by_digest.setdefault(digest, []).append(str(path))
    return [paths for paths in by_digest.values() if len(paths) > 1], unreadable


def record_own_reads(conn, host: str, reads: dict[str, float]) -> None:
    """Remember the atimes cdm's own full reads left behind. See scan.atime_for."""
    conn.executemany(
        "UPDATE files SET self_atime = ? WHERE host = ? AND path = ? "
        "AND atime IS NOT NULL",
        [(t, host, p) for p, t in reads.items()])
    conn.commit()


def disk_usage(conn, under: str, *, depth: int = 1, host=None, limit: int = 40):
    """Sum file sizes by subdirectory, `du -d N` answered from the index.

    Rolled up in Python rather than SQL because the grouping key is "the path
    component `depth` levels below `under`", which SQL cannot express without
    recursion. Rows stream, so memory is bounded by the number of distinct
    subdirectories rather than by the number of files.

    Directory rows are excluded from the sum -- a directory's own inode size is
    not the space its contents occupy, and counting both double-counts.
    """
    base = Path(under).expanduser().resolve()
    prefix = str(base).rstrip("/") + "/"
    clauses = ["type = 'file'", "path LIKE ? ESCAPE '\\'"]
    params: list = [_like_prefix(prefix)]
    if host:
        clauses.append("host = ?")
        params.append(host)

    sql = f"SELECT path, size FROM files WHERE {' AND '.join(clauses)}"
    totals: dict[str, list[int]] = {}
    for path, size in conn.execute(sql, params):
        rest = path[len(prefix):].split("/")
        key = str(base.joinpath(*rest[:depth])) if len(rest) > depth else path
        entry = totals.setdefault(key, [0, 0])
        entry[0] += size
        entry[1] += 1

    rows = [{"path": k, "bytes": v[0], "files": v[1]} for k, v in totals.items()]
    rows.sort(key=lambda r: r["bytes"], reverse=True)
    return rows[:limit]


def _like_prefix(prefix: str) -> str:
    """Escape a path for use as a LIKE prefix.

    A path containing % or _ would otherwise turn into a wildcard and match
    directories that merely resemble it.
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


@dataclass
class Forgotten:
    removed: int = 0
    known: bool = False
    # Rows handed to the root that encloses the forgotten one: it still covers
    # them, so deleting them would only mean re-hashing them on its next scan.
    handed_over: int = 0
    handed_to: str | None = None
    # Roots nested inside the forgotten one, which keep their own rows.
    nested: list[str] = field(default_factory=list)


def forget_root(conn, path: str, host: str) -> Forgotten:
    """Drop a root and the rows it owns.

    Rows owned by a root nested inside it are left alone; rows the forgotten
    root owned that an enclosing root also covers are handed to that root
    rather than deleted. Touches only the index; nothing on disk is read or
    written.
    """
    root_key = str(Path(path).expanduser().resolve())
    out = Forgotten()
    out.known = conn.execute(
        "SELECT 1 FROM roots WHERE host = ? AND path = ?", (host, root_key)
    ).fetchone() is not None
    if not out.known:
        return out

    out.nested = roots.nested(conn, host, root_key)
    out.handed_to = roots.enclosing(conn, host, root_key)
    if out.handed_to is not None:
        out.handed_over = max(conn.execute(
            "UPDATE files SET root = ? WHERE host = ? AND root = ?",
            (out.handed_to, host, root_key)).rowcount, 0)
    else:
        out.removed = max(conn.execute(
            "DELETE FROM files WHERE host = ? AND root = ?", (host, root_key)
        ).rowcount, 0)
    conn.execute("DELETE FROM roots WHERE host = ? AND path = ?", (host, root_key))
    conn.commit()
    return out


def stat_one(conn, path: str, host=None):
    clauses, params = ["path = ?"], [str(Path(path).expanduser().resolve())]
    if host:
        clauses.append("host = ?")
        params.append(host)
    sql = f"SELECT * FROM files WHERE {' AND '.join(clauses)}"
    return conn.execute(sql, params).fetchone()


def hash_is_stale(row) -> bool:
    """True when the file changed after its hash was taken."""
    if row["hash"] is None:
        return False
    return row["hash_size"] != row["size"] or row["hash_mtime"] != row["mtime"]
