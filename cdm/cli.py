"""The command line.

Verbs: scan, rescan, roots, forget, find, du, dupes, stat, doctor.

Output goes to stdout as plain columns; anything the user did not ask for --
skip counts, warnings, timings -- goes to stderr, so `cdm find ... | xargs` and
`$(cdm find -q ...)` stay clean.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import sys
import time
from contextlib import closing
from pathlib import Path

from . import db, hashing, paths, query
from .exclude import Excluder
from .scan import scan_root

__version__ = "0.1.0"


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def with_index(fn):
    """Open the index for a command and always close it again.

    Process exit would close it anyway, but leaving connections to be finalised
    by the garbage collector means WAL checkpoints happen at an unpredictable
    moment -- and it makes every test emit an unraisable-exception warning that
    would drown a real one.
    """
    @functools.wraps(fn)
    def wrapper(args):
        with closing(db.connect()) as conn:
            return fn(args, conn)
    return wrapper


def human_size(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}T"


def human_time(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def _rows_out(rows, as_json: bool, quiet: bool) -> None:
    if as_json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return
    if quiet:
        for r in rows:
            print(r["path"])
        return
    if not rows:
        _err("no matches")
        return
    width = max(len(human_size(r["size"])) for r in rows)
    for r in rows:
        flag = " *stale" if query.hash_is_stale(r) else ""
        print(f"{human_size(r['size']):>{width}}  {human_time(r['mtime'])}  "
              f"{r['path']}{flag}")


def _hash_kind(args) -> str | None:
    if getattr(args, "full_checksum", False):
        return hashing.FULL
    if getattr(args, "checksum", False):
        return hashing.PARTIAL
    return None


def _excluder(args) -> Excluder:
    return Excluder(extra=tuple(getattr(args, "exclude", []) or []),
                    skip_credentials=not getattr(args, "no_skip_credentials", False))


def _progress_printer():
    """A progress callback, but only when someone is watching.

    Writing \\r-updated counters into a log file or a CI transcript produces
    thousands of useless lines, so this is a no-op unless stderr is a terminal.
    """
    if not sys.stderr.isatty():
        return None

    def show(stats) -> None:
        print(f"\r  scanned {stats.total:,} entries...", end="", file=sys.stderr,
              flush=True)
    return show


def _clear_progress() -> None:
    if sys.stderr.isatty():
        print("\r" + " " * 40 + "\r", end="", file=sys.stderr)


def _report_scan(root: Path, stats, ex: Excluder) -> None:
    _clear_progress()
    _err(f"{root}: {stats.files} files, {stats.dirs} dirs, {stats.links} links "
         f"in {stats.elapsed:.1f}s")
    if stats.hashed or stats.reused_hashes:
        _err(f"  hashed {stats.hashed}, reused {stats.reused_hashes} unchanged")
    if stats.pruned:
        _err(f"  dropped {stats.pruned} row(s) for files no longer on disk")
    for line in ex.report():
        _err(f"  {line}")
    if stats.unreadable:
        _err(f"  {len(stats.unreadable)} path(s) unreadable, first: {stats.unreadable[0]}")


@with_index
def cmd_scan(args, conn) -> int:
    host = paths.this_host()
    ex = _excluder(args)
    kind = _hash_kind(args)
    cap = query.parse_size(args.max_hash_size) if args.max_hash_size else None

    rc = 0
    for raw in args.paths:
        root = Path(raw).expanduser()
        if not root.is_dir():
            _err(f"cdm: not a directory: {root}")
            rc = 2
            continue
        stats = scan_root(conn, host, root, hash_kind=kind, max_hash_bytes=cap,
                          excluder=ex, progress=_progress_printer())
        _report_scan(root.resolve(), stats, ex)
    return rc


@with_index
def cmd_rescan(args, conn) -> int:
    host = paths.this_host()
    ex = _excluder(args)
    kind = _hash_kind(args)
    cap = query.parse_size(args.max_hash_size) if args.max_hash_size else None

    if args.paths:
        wanted = [str(Path(p).expanduser().resolve()) for p in args.paths]
    else:
        wanted = [r["path"] for r in
                  conn.execute("SELECT path FROM roots WHERE host = ? ORDER BY path",
                               (host,))]
    if not wanted:
        _err("cdm: no roots to rescan. Add one with `cdm scan <path>`.")
        return 3

    rc = 0
    for path in wanted:
        root = Path(path)
        if not root.is_dir():
            _err(f"cdm: root has gone away, skipping: {root}")
            rc = 2
            continue
        stats = scan_root(conn, host, root, hash_kind=kind, max_hash_bytes=cap,
                          excluder=ex, progress=_progress_printer())
        _report_scan(root, stats, ex)
    return rc


@with_index
def cmd_roots(args, conn) -> int:
    rows = conn.execute(
        "SELECT r.host, r.path, r.added_at, r.last_scan, "
        "       (SELECT COUNT(*) FROM files f "
        "         WHERE f.host = r.host AND f.root = r.path) AS n "
        "FROM roots r ORDER BY r.host, r.path"
    ).fetchall()
    if not rows:
        _err("no roots yet. Add one with `cdm scan <path>`.")
        return 0
    for r in rows:
        print(f"{r['path']}  ({r['host']})  {r['n']} entries  "
              f"last scan {r['last_scan'] or 'never'}")
    return 0


@with_index
def cmd_find(args, conn) -> int:
    try:
        rows = query.find(
            conn,
            host=None if args.all_hosts else paths.this_host(),
            root=args.root,
            name=args.name,
            kind=args.type,
            larger_than=query.parse_size(args.larger_than) if args.larger_than else None,
            smaller_than=query.parse_size(args.smaller_than) if args.smaller_than else None,
            modified_after=query.parse_when(args.modified_after) if args.modified_after else None,
            modified_before=(query.parse_when(args.modified_before)
                             if args.modified_before else None),
            order=args.order,
            limit=args.limit,
        )
    except ValueError as exc:
        _err(f"cdm: {exc}")
        return 2
    _rows_out(rows, args.json, args.quiet)
    return 0


@with_index
def cmd_dupes(args, conn) -> int:
    try:
        min_size = query.parse_size(args.min_size)
    except ValueError as exc:
        _err(f"cdm: {exc}")
        return 2

    groups = query.dupe_groups(conn, host=None if args.all_hosts else paths.this_host(),
                               min_size=min_size, limit=args.limit)
    if not groups:
        _err("no duplicate candidates. Did you scan with --checksum?")
        return 0

    total = 0
    for g in groups:
        if args.verify and g["hash_kind"] == hashing.PARTIAL:
            confirmed = query.verify_group(g)
            if not confirmed:
                continue
            for members in confirmed:
                total += g["size"] * (len(members) - 1)
                print(f"{human_size(g['size'])} x{len(members)}  (verified)")
                for path in members:
                    print(f"    {path}")
        else:
            total += g["reclaimable"]
            suffix = ("" if g["hash_kind"] == hashing.FULL
                      else "  (partial hash, --verify to confirm)")
            print(f"{human_size(g['size'])} x{g['count']}{suffix}")
            for row in g["members"]:
                print(f"    {row['path']}")
    _err(f"reclaimable: {human_size(total)}")
    return 0


@with_index
def cmd_du(args, conn) -> int:
    rows = query.disk_usage(conn, args.path, depth=args.depth,
                            host=None if args.all_hosts else paths.this_host(),
                            limit=args.limit)
    if not rows:
        _err(f"cdm: nothing indexed under {args.path}. Scan it first.")
        return 1
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    width = max(len(human_size(r["bytes"])) for r in rows)
    for r in rows:
        print(f"{human_size(r['bytes']):>{width}}  {r['files']:>8} files  {r['path']}")
    total = sum(r["bytes"] for r in rows)
    _err(f"total shown: {human_size(total)}")
    return 0


@with_index
def cmd_forget(args, conn) -> int:
    host = paths.this_host()
    removed, known = query.forget_root(conn, args.path, host)
    if not known:
        _err(f"cdm: not a known root: {args.path}")
        _err("     `cdm roots` lists what is indexed.")
        return 1
    _err(f"forgot {args.path}: {removed} row(s) removed from the index "
         f"(nothing on disk was touched)")
    return 0


@with_index
def cmd_stat(args, conn) -> int:
    row = query.stat_one(conn, args.path, host=paths.this_host())
    if row is None:
        _err(f"cdm: not in the index: {args.path}")
        return 1
    if args.json:
        print(json.dumps(dict(row), indent=2))
        return 0
    print(f"path      {row['path']}")
    print(f"host      {row['host']}")
    print(f"root      {row['root']}")
    print(f"type      {row['type']}")
    print(f"size      {human_size(row['size'])}  ({row['size']} bytes)")
    print(f"modified  {human_time(row['mtime'])}")
    print(f"created   {human_time(row['ctime'])}")
    print(f"inode     {row['inode']}")
    if row["hash"]:
        stale = "  STALE (file changed since it was hashed)" if query.hash_is_stale(row) else ""
        print(f"hash      {row['hash']}  [{row['hash_kind']}]{stale}")
    else:
        print("hash      none recorded (scan with --checksum)")
    print(f"seen      {row['seen_at']}")
    return 0


def cmd_doctor(args) -> int:
    index = paths.index_path()
    print(f"index     {index}")
    if not index.exists():
        _err("cdm: no index yet. Run `cdm scan <path>`.")
        return 1

    mode = os.stat(index).st_mode & 0o777
    print(f"mode      {oct(mode)}" + ("" if mode == 0o600 else "   <- expected 0600"))
    print(f"size      {human_size(index.stat().st_size)}")

    # Opened here rather than through @with_index: connecting creates the file,
    # which would turn "you have no index" into "you have an empty index".
    with closing(db.connect()) as conn:
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        hashed = conn.execute(
            "SELECT COUNT(*) FROM files WHERE hash IS NOT NULL").fetchone()[0]
        stale = conn.execute(
            "SELECT COUNT(*) FROM files WHERE hash IS NOT NULL "
            "AND (hash_size != size OR hash_mtime != mtime)"
        ).fetchone()[0]
        roots = conn.execute(
            "SELECT host, path, last_scan FROM roots ORDER BY path").fetchall()

    print(f"entries   {files}  ({hashed} hashed, {stale} stale)")
    print(f"roots     {len(roots)}")
    rc = 0
    for r in roots:
        gone = "" if Path(r["path"]).is_dir() else "   <- gone from disk"
        if gone:
            rc = 1
        print(f"  {r['path']}  last scan {r['last_scan'] or 'never'}{gone}")
    if stale:
        _err(f"cdm: {stale} hash(es) are stale; `cdm rescan --checksum` refreshes them")
    return rc


def _add_scan_flags(p) -> None:
    p.add_argument("--checksum", action="store_true",
                   help="record a partial hash (ends + size) for dedupe")
    p.add_argument("--full-checksum", action="store_true",
                   help="record a full-content hash, for integrity rather than dedupe")
    p.add_argument("--max-hash-size", metavar="SIZE",
                   help="skip hashing files bigger than this (e.g. 2G)")
    p.add_argument("--exclude", action="append", metavar="GLOB",
                   help="skip paths matching this glob (repeatable)")
    p.add_argument("--no-skip-credentials", action="store_true",
                   help="index credential paths too (off by default, on purpose)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cdm",
        description="A local file metadata catalog.",
        epilog="examples:\n"
               "  cdm scan ~/work --checksum\n"
               "  cdm find --larger-than 100M --modified-after 7d\n"
               "  cdm dupes --min-size 10M --verify\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"cdm {__version__}")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("scan", help="add a root and index it")
    s.add_argument("paths", nargs="+")
    _add_scan_flags(s)
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("rescan", help="re-index known roots, reusing unchanged hashes")
    r.add_argument("paths", nargs="*")
    _add_scan_flags(r)
    r.set_defaults(func=cmd_rescan)

    sub.add_parser("roots", help="list what is being watched").set_defaults(func=cmd_roots)

    f = sub.add_parser("find", help="query the index")
    f.add_argument("--name", metavar="GLOB", help="match the filename, e.g. '*.csv'")
    f.add_argument("--type", choices=["file", "dir", "link"])
    f.add_argument("--larger-than", metavar="SIZE")
    f.add_argument("--smaller-than", metavar="SIZE")
    f.add_argument("--modified-after", metavar="WHEN", help="7d, 24h, or 2026-08-01")
    f.add_argument("--modified-before", metavar="WHEN")
    f.add_argument("--root", metavar="PATH", help="restrict to one root")
    f.add_argument("--order", choices=["size", "mtime", "name", "path"], default="size")
    f.add_argument("--limit", type=int, default=100)
    f.add_argument("--all-hosts", action="store_true")
    f.add_argument("-q", "--quiet", action="store_true", help="paths only, for piping")
    f.add_argument("--json", action="store_true")
    f.set_defaults(func=cmd_find)

    d = sub.add_parser("dupes", help="files that look identical")
    d.add_argument("--min-size", metavar="SIZE", default="1")
    d.add_argument("--limit", type=int, default=100)
    d.add_argument("--all-hosts", action="store_true")
    d.add_argument("--verify", action="store_true",
                   help="re-hash partial-hash candidates in full to confirm")
    d.set_defaults(func=cmd_dupes)

    u = sub.add_parser("du", help="disk usage by subdirectory, answered from the index")
    u.add_argument("path", nargs="?", default=".")
    u.add_argument("-d", "--depth", type=int, default=1,
                   help="how many levels below PATH to group at (default 1)")
    u.add_argument("--limit", type=int, default=40)
    u.add_argument("--all-hosts", action="store_true")
    u.add_argument("--json", action="store_true")
    u.set_defaults(func=cmd_du)

    g = sub.add_parser("forget", help="drop a root and its rows from the index")
    g.add_argument("path")
    g.set_defaults(func=cmd_forget)

    t = sub.add_parser("stat", help="what the index knows about one path")
    t.add_argument("path")
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_stat)

    sub.add_parser("doctor", help="index health").set_defaults(func=cmd_doctor)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _err("\ncdm: interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
