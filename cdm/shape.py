"""The shape of the data, without the names in it.

Everything here answers "how much, how old, what kind" and nothing here returns
a path below a root. That is the contract that lets these functions be exposed
by default to a model client (see docs/adr/0002): a size histogram of your disk
reveals what is eating it, not what anything is called.

Root paths themselves do appear -- you typed them -- but no string derived from
anything beneath a root does, with one deliberately narrow exception: file
extensions, filtered so that an "extension" cannot smuggle out a name (see
extension_of).

Stdlib only, and in core rather than in the MCP extra, so that a future
`cdm export --shape` can reuse exactly these functions.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from . import roots as roots_mod

KB, MB, GB = 1024, 1024 ** 2, 1024 ** 3

# (label, upper bound exclusive). Binary units, matching parse_size and df.
SIZE_BUCKETS = (
    ("<4K", 4 * KB),
    ("4K-1M", MB),
    ("1M-100M", 100 * MB),
    ("100M-1G", GB),
    ("1G-10G", 10 * GB),
    (">=10G", None),
)

DAY = 86400
AGE_BUCKETS = (
    ("<7d", 7 * DAY),
    ("7d-30d", 30 * DAY),
    ("30d-90d", 90 * DAY),
    ("90d-1y", 365 * DAY),
    ("1y-3y", 3 * 365 * DAY),
    (">=3y", None),
)
FUTURE = "future"   # mtime ahead of the clock: reported, never silently folded in

NO_EXTENSION = "(none)"
OTHER_EXTENSION = "(other)"
_EXTENSION = re.compile(r"^[a-z0-9]{1,8}$")
MIN_EXTENSION_FILES = 2


def _scope(host: str | None, root: str | None, *, table: str = "") -> tuple[str, list]:
    prefix = f"{table}." if table else ""
    clauses, params = [], []
    if host is not None:
        clauses.append(f"{prefix}host = ?")
        params.append(host)
    if root is not None:
        # The root and every root nested inside it: see roots.py.
        clauses.append(roots_mod.scope_sql(f"{prefix}root"))
        params.extend(roots_mod.scope_params(root))
    return (" AND ".join(clauses) or "1=1"), params


def human(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T", "P"):
        if n < 1024 or unit == "P":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}P"


def summary(conn, *, host: str | None = None, root: str | None = None,
            now: float | None = None) -> dict:
    """Per-root totals and how stale each root's last scan is."""
    now = time.time() if now is None else now
    where, params = _scope(host, root)
    counts: dict[str, dict] = {}
    for r in conn.execute(
        f"SELECT root, type, COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes "
        f"FROM files WHERE {where} GROUP BY root, type", params
    ):
        entry = counts.setdefault(r["root"], {"files": 0, "dirs": 0, "links": 0,
                                              "bytes": 0})
        key = {"file": "files", "dir": "dirs", "link": "links"}[r["type"]]
        entry[key] = r["n"]
        if r["type"] == "file":
            # Directory inode sizes are not the space their contents occupy.
            entry["bytes"] = r["bytes"]

    # Scoped to a root, the answer lists it and the roots nested inside it. Each
    # row is counted once, under its most specific root, so the totals add up.
    rwhere, rparams = _scope(host, None)
    if root is not None:
        rwhere += " AND " + roots_mod.scope_sql("path")
        rparams.extend(roots_mod.scope_params(root))
    roots = []
    for r in conn.execute(
        f"SELECT host, path, last_scan FROM roots WHERE {rwhere} ORDER BY path", rparams
    ):
        c = counts.get(r["path"], {"files": 0, "dirs": 0, "links": 0, "bytes": 0})
        age_days = None
        if r["last_scan"]:
            scanned = datetime.fromisoformat(r["last_scan"]).timestamp()
            age_days = round((now - scanned) / DAY, 2)
        roots.append({"root": r["path"], "host": r["host"], **c,
                      "size": human(c["bytes"]),
                      # Set when this root is nested in another, whose counts
                      # then leave this one's rows out.
                      "inside": roots_mod.enclosing(conn, r["host"], r["path"]),
                      "last_scan": r["last_scan"], "last_scan_age_days": age_days})

    total_files = sum(r["files"] for r in roots)
    total_bytes = sum(r["bytes"] for r in roots)
    return {"roots": roots, "total_files": total_files, "total_bytes": total_bytes,
            "total_size": human(total_bytes)}


def size_histogram(conn, *, host: str | None = None, root: str | None = None) -> dict:
    """File counts and bytes per size bucket. Every bucket is present, zeros too."""
    where, params = _scope(host, root)
    case = " ".join(
        f"WHEN size < {upper} THEN {i}" for i, (_, upper) in enumerate(SIZE_BUCKETS)
        if upper is not None)
    rows = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT CASE {case} ELSE {len(SIZE_BUCKETS) - 1} END AS b, COUNT(*), "
        f"COALESCE(SUM(size), 0) FROM files WHERE type = 'file' AND {where} GROUP BY b",
        params)}
    buckets = []
    for i, (label, _) in enumerate(SIZE_BUCKETS):
        n, b = rows.get(i, (0, 0))
        buckets.append({"bucket": label, "files": n, "bytes": b, "size": human(b)})
    return {"buckets": buckets, "total_files": sum(x["files"] for x in buckets),
            "total_bytes": sum(x["bytes"] for x in buckets)}


def age_histogram(conn, *, host: str | None = None, root: str | None = None,
                  now: float | None = None) -> dict:
    """File counts and bytes by time since last modification.

    Files whose mtime is in the future (clock skew, a restored archive) get
    their own bucket rather than being quietly counted as "recent".
    """
    now = time.time() if now is None else now
    where, params = _scope(host, root)
    whens = [f"WHEN mtime > {now!r} THEN -1"]
    for i, (_, span) in enumerate(AGE_BUCKETS):
        if span is not None:
            whens.append(f"WHEN mtime >= {now - span!r} THEN {i}")
    rows = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT CASE {' '.join(whens)} ELSE {len(AGE_BUCKETS) - 1} END AS b, "
        f"COUNT(*), COALESCE(SUM(size), 0) FROM files "
        f"WHERE type = 'file' AND {where} GROUP BY b", params)}
    buckets = []
    for i, (label, _) in enumerate(AGE_BUCKETS):
        n, b = rows.get(i, (0, 0))
        buckets.append({"bucket": label, "files": n, "bytes": b, "size": human(b)})
    n, b = rows.get(-1, (0, 0))
    buckets.append({"bucket": FUTURE, "files": n, "bytes": b, "size": human(b)})
    return {"buckets": buckets, "measured_from": "mtime",
            "total_files": sum(x["files"] for x in buckets),
            "total_bytes": sum(x["bytes"] for x in buckets)}


def extension_of(name: str) -> str:
    """The extension of a filename, or a placeholder when it is not safe to show.

    An "extension" is whatever follows the last dot, which means a file called
    `notes.JohnSmith` would report JohnSmith. Only short alphanumeric suffixes
    are treated as extensions; anything else becomes (other). Case is folded
    because .JPG and .jpg are the same kind of file.
    """
    lowered = name.lower()
    dot = lowered.rfind(".")
    if dot <= 0:            # no dot at all, or a dotfile like .bashrc
        return NO_EXTENSION
    suffix = lowered[dot + 1:]
    return suffix if _EXTENSION.match(suffix) else OTHER_EXTENSION


def extensions(conn, *, host: str | None = None, root: str | None = None,
               limit: int = 20) -> dict:
    """Top extensions by bytes. Totals always add up to the whole.

    An extension carried by only one file goes to (other): a one-off suffix is
    more likely to be part of a name than a kind of data. Whatever falls outside
    the top `limit` is summed into a remainder row rather than dropped, so the
    numbers never silently stop adding up.
    """
    where, params = _scope(host, root)
    tally: dict[str, list[int]] = {}
    for name, size in conn.execute(
        f"SELECT name, size FROM files WHERE type = 'file' AND {where}", params
    ):
        entry = tally.setdefault(extension_of(name), [0, 0])
        entry[0] += 1
        entry[1] += size

    folded: dict[str, list[int]] = {}
    for ext, (n, b) in tally.items():
        key = ext
        if ext not in (NO_EXTENSION, OTHER_EXTENSION) and n < MIN_EXTENSION_FILES:
            key = OTHER_EXTENSION
        entry = folded.setdefault(key, [0, 0])
        entry[0] += n
        entry[1] += b

    ranked = sorted(folded.items(), key=lambda kv: kv[1][1], reverse=True)
    shown, rest = ranked[:max(0, limit)], ranked[max(0, limit):]
    rows = [{"extension": ext, "files": n, "bytes": b, "size": human(b)}
            for ext, (n, b) in shown]
    remainder = None
    if rest:
        rn, rb = sum(v[0] for _, v in rest), sum(v[1] for _, v in rest)
        remainder = {"extensions": len(rest), "files": rn, "bytes": rb,
                     "size": human(rb)}
    return {"extensions": rows, "remainder": remainder,
            "total_files": sum(v[0] for v in folded.values()),
            "total_bytes": sum(v[1] for v in folded.values())}


def duplicates_summary(conn, *, host: str | None = None,
                       root: str | None = None) -> dict:
    """How much looks duplicated, and how much of that is actually confirmed.

    Coverage is reported alongside, because "no duplicates" from an index where
    nothing was hashed means nothing at all.
    """
    where, params = _scope(host, root)
    by_kind = {}
    for r in conn.execute(
        f"SELECT hash_kind, COUNT(*) AS groups, SUM(n) AS files, "
        f"       SUM(size * (n - 1)) AS reclaimable "
        f"FROM (SELECT hash_kind, hash, size, COUNT(*) AS n FROM files "
        f"      WHERE hash IS NOT NULL AND size >= 1 AND {where} "
        f"      GROUP BY hash_kind, hash, size HAVING n > 1) "
        f"GROUP BY hash_kind", params
    ):
        by_kind[r["hash_kind"]] = {
            "groups": r["groups"], "files": r["files"],
            "reclaimable_bytes": r["reclaimable"],
            "reclaimable": human(r["reclaimable"])}

    files = conn.execute(
        f"SELECT COUNT(*) FROM files WHERE type = 'file' AND {where}", params
    ).fetchone()[0]
    hashed = dict(conn.execute(
        f"SELECT hash_kind, COUNT(*) FROM files "
        f"WHERE type = 'file' AND hash IS NOT NULL AND {where} GROUP BY hash_kind",
        params).fetchall())
    stale = conn.execute(
        f"SELECT COUNT(*) FROM files WHERE hash IS NOT NULL "
        f"AND (hash_size != size OR hash_mtime != mtime) AND {where}", params
    ).fetchone()[0]

    empty = {"groups": 0, "files": 0, "reclaimable_bytes": 0, "reclaimable": "0B"}
    return {
        "candidates": by_kind.get("partial", empty),
        "candidates_note": "partial hash: probably identical; confirm with "
                           "`cdm dupes --verify`",
        "confirmed": by_kind.get("full", empty),
        "confirmed_note": "full hash: identical content",
        "coverage": {"files": files,
                     "partial_hashed": hashed.get("partial", 0),
                     "full_hashed": hashed.get("full", 0),
                     "stale_hashes": stale},
    }
