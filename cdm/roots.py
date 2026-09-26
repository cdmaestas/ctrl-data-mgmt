"""Nested roots: which root a row belongs to, and what a root covers.

Roots may overlap -- `~` and `~/src` both registered is a reasonable thing to
want. Every row is then owned by the MOST SPECIFIC root that contains it, no
matter which scan wrote it last. `files` is keyed by (host, path), so a path has
exactly one row and one owner; the alternative, "whoever scanned last", made
each scan steal the other root's rows. Observed: `cdm rescan --checksum` hashed
all 95,357 files under ~/Documents/GitHub again right after the ~ scan had
hashed them, because the GitHub scan looked for reusable hashes only among rows
labelled with its own root.

So the `root` column answers "whose rows are these", and a question about
everything under a root -- reusing hashes, `find --root`, the MCP shape tools --
must also take in the roots nested inside it.
"""
from __future__ import annotations


def bounds(root_key: str) -> tuple[str, str]:
    """(lo, hi) such that lo <= path < hi is exactly "path is below root_key".

    A range rather than LIKE: it needs no escaping for `%` and `_`, and SQLite
    can answer it from the (host, path) primary key. '0' is the character after
    '/', so `/data` does not swallow `/data-archive`.
    """
    base = root_key.rstrip("/")
    return base + "/", base + "0"


def nested(conn, host: str, root_key: str) -> list[str]:
    """Registered roots strictly inside `root_key`, most specific first."""
    lo, hi = bounds(root_key)
    found = [r[0] for r in conn.execute(
        "SELECT path FROM roots WHERE host = ? AND path >= ? AND path < ?",
        (host, lo, hi))]
    return sorted(found, key=len, reverse=True)


def enclosing(conn, host: str, root_key: str) -> str | None:
    """The most specific registered root strictly containing `root_key`."""
    best = None
    for (path,) in conn.execute("SELECT path FROM roots WHERE host = ?", (host,)):
        if root_key.startswith(bounds(path)[0]) and (best is None or len(path) > len(best)):
            best = path
    return best


def owner(directory: str, root_key: str, inner: list[str]) -> str:
    """Which root owns the entries of `directory`.

    `inner` is nested(root_key), most specific first. Ownership is decided by
    the parent directory, so the directory that IS a nested root is itself a
    row of the enclosing root -- the nested root's own scan never records it.
    """
    for candidate in inner:
        if directory == candidate or directory.startswith(bounds(candidate)[0]):
            return candidate
    return root_key


def scope_sql(column: str = "root") -> str:
    """SQL for "owned by this root or by a root nested inside it".

    Takes three parameters: see scope_params().
    """
    return (f"({column} = ? OR {column} IN "
            f"(SELECT path FROM roots WHERE path >= ? AND path < ?))")


def scope_params(root_key: str) -> list[str]:
    return [root_key, *bounds(root_key)]
