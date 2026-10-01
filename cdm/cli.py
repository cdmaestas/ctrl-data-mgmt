"""The command line.

Verbs: scan, rescan, import, hash, roots, forget, find, du, dupes, storage, suggest,
guide, policy, stat, doctor, mcp.

Output goes to stdout as plain columns; anything the user did not ask for --
skip counts, warnings, timings -- goes to stderr, so `cdm find ... | xargs` and
`$(cdm find -q ...)` stay clean.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import shutil
import sys
import textwrap
import time
from contextlib import closing
from pathlib import Path

from . import (
    atime,
    db,
    guide,
    hasher,
    hashing,
    importer,
    paths,
    policy,
    probe,
    query,
    roots,
    shape,
    suggest,
)
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


def _visible(conn) -> tuple[str, ...]:
    """What a question covers: this machine plus imported listings (roots.py)."""
    return roots.visible_hosts(conn, paths.this_host())


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


def human_duration(secs: float) -> str:
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{secs % 3600 // 60:02d}m"


def _progress_line(stats) -> str:
    per_sec, bytes_per_sec = stats.rates()
    line = f"scanned {stats.total:,} entries ({per_sec:,.0f}/s)"
    if stats.hashed:
        line += (f", hashed {human_size(stats.hashed_bytes)} "
                 f"({human_size(bytes_per_sec)}/s)")
    return line + f", {human_duration(stats.running_for)}"


def _progress_printer(every: float | None = None):
    """A progress callback, but only when someone is watching -- or asked.

    Writing \\r-updated counters into a log file or a CI transcript produces
    thousands of useless lines, so without `every` this is a no-op unless
    stderr is a terminal. With `every` (--progress), one timestamped line is
    written per interval wherever stderr goes, which is what a background scan
    or a log needs.
    """
    if every is not None:
        last = [0.0]

        def log(stats) -> None:
            now = time.time()
            if now - last[0] >= every:
                last[0] = now
                _err(f"  {time.strftime('%H:%M:%S')}  {_progress_line(stats)}")
        return log

    if not sys.stderr.isatty():
        return None

    def show(stats) -> None:
        # \x1b[K clears whatever a longer previous line left behind.
        print(f"\r  {_progress_line(stats)}\x1b[K", end="", file=sys.stderr,
              flush=True)
    return show


def _clear_progress() -> None:
    if sys.stderr.isatty():
        print("\r\x1b[K", end="", file=sys.stderr)


def _report_scan(root: Path, stats, ex: Excluder) -> None:
    _clear_progress()
    if stats.root_unreadable:
        # Not "0 files": the scan failed. Saying it plainly, and naming the
        # usual causes, because the symptom looks identical to an empty tree.
        _err(f"cdm: FAILED {root}: could not read the directory itself, so "
             f"nothing was indexed and nothing was removed.")
        _err("     Check permissions, whether the filesystem is mounted, and on "
             "macOS whether the path needs Full Disk Access.")
        return
    per_sec, bytes_per_sec = stats.rates()
    _err(f"{root}: {stats.files} files, {stats.dirs} dirs, {stats.links} links "
         f"in {stats.elapsed:.1f}s ({per_sec:,.0f} entries/s)")
    if stats.threads > 1:
        _err(f"  {stats.threads} walker threads")
    if stats.resumed_from:
        _err(f"  resumed from a checkpoint: {stats.resumed_from} "
             f"director{'y' if stats.resumed_from == 1 else 'ies'} already done")
    if stats.atime_note:
        _err(f"  access times: not measured ({stats.atime_note}); judged from mount "
             f"options instead")
    if stats.hashed or stats.reused_hashes:
        _err(f"  hashed {stats.hashed} ({human_size(stats.hashed_bytes)} read, "
             f"{human_size(bytes_per_sec)}/s), reused {stats.reused_hashes} unchanged")
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
        root_key = str(root.resolve())
        is_new = conn.execute("SELECT 1 FROM roots WHERE host = ? AND path = ?",
                              (host, root_key)).fetchone() is None
        stats = scan_root(conn, host, root, hash_kind=kind, max_hash_bytes=cap,
                          excluder=ex, progress=_progress_printer(args.progress),
                          threads=_threads_for(args, root),
                          resume=not args.restart)
        _report_scan(root.resolve(), stats, ex)
        if stats.root_unreadable:
            rc = 1
        elif is_new:
            _report_nesting(conn, host, root_key)
    return rc


def _report_nesting(conn, host: str, root_key: str) -> None:
    """Say so when a new root overlaps an old one. Legal, but not free."""
    outer = roots.enclosing(conn, host, root_key)
    if outer:
        _err(f"  inside root {outer}: entries below here now belong to this root, "
             f"and `cdm rescan` walks them twice (hashes are reused)")
    for inner in roots.nested(conn, host, root_key):
        _err(f"  contains root {inner}, which keeps its own entries")
    if outer or roots.nested(conn, host, root_key):
        _err("  `cdm forget` either root to stop the overlap")


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
                          excluder=ex, progress=_progress_printer(args.progress),
                          threads=_threads_for(args, root),
                          resume=not args.restart)
        _report_scan(root, stats, ex)
        if stats.root_unreadable:
            rc = 1
    return rc


@with_index
def cmd_roots(args, conn) -> int:
    rows = conn.execute(
        "SELECT r.host, r.path, r.added_at, r.last_scan, r.source, "
        "       (SELECT COUNT(*) FROM files f "
        "         WHERE f.host = r.host AND f.root = r.path) AS n "
        "FROM roots r ORDER BY r.host, r.path"
    ).fetchall()
    if not rows:
        _err("no roots yet. Add one with `cdm scan <path>`.")
        return 0
    for r in rows:
        # Entries count once, under their most specific root: a root's count
        # leaves out whatever a root nested in it owns.
        outer = roots.enclosing(conn, r["host"], r["path"])
        inside = f"  (inside {outer})" if outer else ""
        if r["source"] == roots.POLICY:
            print(f"{r['path']}  ({r['host']}, imported listing)  {r['n']} entries  "
                  f"imported {r['last_scan'] or 'never'}{inside}")
        else:
            print(f"{r['path']}  ({r['host']})  {r['n']} entries  "
                  f"last scan {r['last_scan'] or 'never'}{inside}")
    return 0


@with_index
def cmd_find(args, conn) -> int:
    try:
        rows = query.find(
            conn,
            host=None if args.all_hosts else _visible(conn),
            root=args.root,
            name=args.name,
            iname=args.iname,
            kind=args.type,
            larger_than=query.parse_size(args.larger_than) if args.larger_than else None,
            smaller_than=query.parse_size(args.smaller_than) if args.smaller_than else None,
            modified_after=query.parse_when(args.modified_after) if args.modified_after else None,
            modified_before=(query.parse_when(args.modified_before)
                             if args.modified_before else None),
            accessed_after=query.parse_when(args.accessed_after) if args.accessed_after else None,
            accessed_before=(query.parse_when(args.accessed_before)
                             if args.accessed_before else None),
            unopened=args.unopened,
            fileset=args.fileset,
            pool=args.pool,
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

    groups = query.dupe_groups(conn, host=None if args.all_hosts else _visible(conn),
                               min_size=min_size, limit=args.limit)
    if not groups:
        _err("no duplicate candidates. Did you scan with --checksum?")
        return 0

    total = 0
    unreadable: list[str] = []
    reads: dict[str, float] = {}
    for g in groups:
        if args.verify and g["hash_kind"] == hashing.PARTIAL:
            confirmed, skipped = query.verify_group(g, reads)
            unreadable.extend(skipped)
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
    if reads:
        blind = query.record_own_reads(conn, paths.this_host(), reads)
        if blind:
            _err(f"cdm: could not read back the access time of {len(blind)} file(s) "
                 f"after verifying them, so the next scan may count cdm's read as "
                 f"a use; first: {blind[0]}")
    if unreadable:
        # Never silent: a verification that could not read a file has not
        # verified anything about it.
        _err(f"cdm: {len(unreadable)} file(s) could not be read during "
             f"verification and are NOT counted, first: {unreadable[0]}")
        return 1
    return 0


@with_index
def cmd_du(args, conn) -> int:
    rows = query.disk_usage(conn, args.path, depth=args.depth,
                            host=None if args.all_hosts else _visible(conn),
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


def _days(text: str) -> int:
    """'90' or '90d' -> 90."""
    try:
        days = int(text[:-1] if text.lower().endswith("d") else text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of days: {text!r}") from None
    if days < 0:
        raise argparse.ArgumentTypeError("days cannot be negative")
    return days


@with_index
def cmd_suggest(args, conn) -> int:
    root = str(Path(args.root).expanduser().resolve()) if args.root else None
    result = suggest.suggest(conn, host=_visible(conn), root=root,
                             older_than_days=args.older_than, limit=args.limit,
                             max_items=10 ** 6 if args.all else args.items,
                             fast_pool=args.fast_pool, cold_pool=args.cold_pool,
                             cold_days=args.cold_after)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    found = result["suggestions"]
    if not found:
        _err("nothing to suggest. If that is surprising, check `cdm roots` and "
             "whether the tree was scanned with --checksum.")
        return 0

    pad = " " * 21
    for n, s in enumerate(found, 1):
        print(f"{n:>2}  {s['risk']:<6}  {s['size']:>7}  {s['title']}")
        print(textwrap.fill(s["detail"], width=max(60, shutil.get_terminal_size().columns),
                            initial_indent=pad, subsequent_indent=pad))
        items = s.get("items", [])
        for item in items:
            when = f", modified {item['newest'][:10]}" if item.get("newest") else ""
            if item.get("last_read"):
                when += f", read {item['last_read'][:10]}"
            if item.get("unopened"):
                when += (f", never opened (as of {item['unopened_as_of'][:10]})"
                         if item["files"] <= 1 else
                         f", {item['unopened']:,} never opened (as of "
                         f"{item['unopened_as_of'][:10]})")
            facts = (f"{item['files']:,} files{when}" if item["files"] > 1
                     else when.lstrip(", "))
            print(f"                     {item['size']:>7}  {item['path']}"
                  + (f"  ({facts})" if facts else ""))
            if item.get("action") and not s["action"]:
                print(f"                              $ {item['action']}")
        if s["item_count"] > len(items):
            print(f"                     ... and {s['item_count'] - len(items)} more "
                  f"(--all to list every one)")
        if s["action"]:
            print(f"                     $ {s['action']}")
        if s.get("draft"):
            for rule_line in s["draft"].rstrip("\n").splitlines():
                print(f"{pad}  {rule_line}")
        print()
    if result["truncated"]:
        _err(f"showing the top {args.limit}; --limit to see more")
    _err(result["note"])
    return 0


@with_index
def cmd_import(args, conn) -> int:
    try:
        stats = importer.import_policy(conn, args.policy, root=args.root,
                                       progress=_progress_printer(args.progress))
    except (OSError, policy.ListingError) as exc:
        _clear_progress()
        _err(f"cdm: {exc}")
        return 1
    _clear_progress()
    per_sec, _ = stats.rates()
    _err(f"{stats.root} ({stats.host}): imported {stats.files} files, {stats.dirs} "
         f"dirs, {stats.links} links in {stats.elapsed:.1f}s ({per_sec:,.0f} entries/s)")
    if stats.pruned:
        _err(f"  removed {stats.pruned} row(s) the listing no longer contains")
    return 0


@with_index
def cmd_hash(args, conn) -> int:
    root = str(Path(args.root).expanduser().resolve()) if args.root else None
    kind = hashing.FULL if args.full else hashing.PARTIAL
    try:
        min_size = query.parse_size(args.min_size)
    except ValueError as exc:
        _err(f"cdm: {exc}")
        return 2
    stats = hasher.hash_files(conn, _visible(conn), root=root, fileset=args.fileset,
                              kind=kind, min_size=min_size,
                              progress=_progress_printer(args.progress))
    _clear_progress()
    _, bytes_per_sec = stats.rates()
    _err(f"hashed {stats.hashed:,} of {stats.candidates:,} size-matched file(s) "
         f"({human_size(stats.hashed_bytes)} read, {human_size(bytes_per_sec)}/s) "
         f"in {stats.running_for:.1f}s")
    if stats.elsewhere:
        _err(f"  {stats.elsewhere:,} not on this machine: run `cdm hash` where they "
             f"are mounted")
    if stats.changed:
        _err(f"  {stats.changed:,} changed since they were indexed: re-import or "
             f"rescan, then hash again")
    if stats.unreadable:
        _err(f"  {len(stats.unreadable):,} unreadable, first: {stats.unreadable[0]}")
        return 1
    return 0


@with_index
def cmd_storage(args, conn) -> int:
    root = str(Path(args.root).expanduser().resolve()) if args.root else None
    # The person at the terminal owns the index, so names are shown here.
    out = shape.storage(conn, host=_visible(conn), root=root, names=True)
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    if not out["pools"] and not out["filesets"]:
        _err("nothing from a Storage Scale listing yet: see `cdm policy` and "
             "`cdm import --policy`")
        return 0
    for title, rows, key in (("pool", out["pools"], "pool"),
                             ("fileset", out["filesets"], "fileset")):
        print(f"{title:<8} {'size':>8}  {'files':>10}")
        for r in rows:
            print(f"{r[key]:<8} {r['size']:>8}  {r['files']:>10,}")
        print()
    rest = out["not_from_a_listing"]
    if rest["files"]:
        _err(f"also {rest['files']:,} scanned file(s), {rest['size']}, with no pool or "
             f"fileset")
    return 0


def cmd_policy(args) -> int:
    """Print the listing script; cdm never runs mmapplypolicy itself."""
    fileset = None if args.whole_filesystem else args.fileset
    try:
        text = policy.script(args.device, fileset, output=args.output,
                             nodes=args.nodes, generator=f"cdm {__version__}")
    except policy.PolicyError as exc:
        _err(f"cdm: {exc}")
        return 2
    print(text, end="")
    _err(policy.instructions(args.device, fileset, args.output))
    return 0


_MODE_MEANS = {
    atime.LAST: "last read recorded, and files never opened since a change",
    atime.FIRST: "files never opened since a change are recorded, dated; last "
                 "read is not",
    atime.NONE: "not recorded",
}

_MARK = {"done": "[x]", "todo": "[ ]", "advice": "[~]", "optional": "[?]"}


def cmd_guide(args) -> int:
    if args.schedule:
        # Needs no index: it only prints a job definition to install yourself.
        # The job goes to stdout so it can be redirected into place as is.
        job, how = guide.schedule()
        print(job, end="")
        _err(how)
        return 0
    return _show_guide(args)


@with_index
def _show_guide(args, conn) -> int:
    result = guide.guide(conn, host=_visible(conn))
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    width = max(60, shutil.get_terminal_size().columns)
    pad = " " * 9
    print(f"Getting started with cdm: {result['done']} of {result['of']} done\n")
    for n, step in enumerate(result["steps"], 1):
        is_next = step["id"] == result["next"]
        mark = "[>]" if is_next else _MARK[step["status"]]
        title = step["title"] + ("  (optional)" if step["status"] == "optional" else "")
        detail = f"  -- {step['detail']}" if step["detail"] else ""
        print(f"  {mark} {n}  {title}{detail}")
        if is_next or args.all:
            for text in (step["why"], f"Then you'll see: {step['expect']}"):
                print(textwrap.fill(text, width=width, initial_indent=pad,
                                    subsequent_indent=pad))
            print(f"{pad}$ {step['command']}\n")
    sys.stdout.flush()
    _err("[x] done  [>] next  [ ] to do  [~] advice the index cannot check  "
         "[?] optional. --all explains every step.")
    return 0


@with_index
def cmd_forget(args, conn) -> int:
    # The root may be this machine's or an imported listing's: find its host.
    key = str(Path(args.path).expanduser().resolve())
    where, params = roots.host_sql(_visible(conn))
    owners = [r[0] for r in conn.execute(
        f"SELECT host FROM roots WHERE path = ? AND {where}", (key, *params))]
    host = owners[0] if len(owners) == 1 else paths.this_host()
    out = query.forget_root(conn, args.path, host)
    if not out.known:
        _err(f"cdm: not a known root: {args.path}")
        _err("     `cdm roots` lists what is indexed.")
        return 1
    if out.handed_to:
        _err(f"forgot {args.path}: its {out.handed_over} row(s) now belong to "
             f"{out.handed_to}, which also covers them; forget that root too to "
             f"drop them (nothing on disk was touched)")
    else:
        _err(f"forgot {args.path}: {out.removed} row(s) removed from the index "
             f"(nothing on disk was touched)")
    for inner in out.nested:
        _err(f"  kept {inner}: a root of its own, with its own rows")
    return 0


@with_index
def cmd_stat(args, conn) -> int:
    row = query.stat_one(conn, args.path, host=_visible(conn))
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
    if row["atime"] is not None:
        print(f"accessed  {human_time(row['atime'])}")
    elif row["type"] == "file":
        print("accessed  not recorded (the filesystem does not update access times, "
              "or it has not been read since cdm last hashed it)")
    if row["fileset"] is not None:
        print(f"fileset   {row['fileset']}  (pool {row['pool']})")
    if row["unopened_until"] is not None:
        print(f"opened    not since it last changed, as of "
              f"{human_time(row['unopened_until'])}")
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

    rc = 0
    # Permissions are checked across the sidecars too: -wal and -shm hold the
    # same filename data as the index, and SQLite creates them itself.
    for path in (index, index.with_name(index.name + "-wal"),
                 index.with_name(index.name + "-shm"), paths.data_dir()):
        if not path.exists():
            continue
        mode = os.stat(path).st_mode & 0o777
        want = paths.DIR_MODE if path.is_dir() else paths.FILE_MODE
        label = "dir " if path.is_dir() else "mode"
        if mode & paths.OTHERS_MASK:
            # Exits non-zero so this can gate a script, rather than being a
            # note in output nobody reads.
            print(f"{label}      {oct(mode)}  <- expected {oct(want)}, "
                  f"OTHERS CAN READ {path.name}")
            rc = 1
        elif path == index:
            print(f"mode      {oct(mode)}")
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
        root_rows = conn.execute(
            "SELECT host, path, last_scan, source, atime_setting FROM roots "
            "ORDER BY path").fetchall()
        regular, with_atime, unopened = conn.execute(
            "SELECT COUNT(*), COUNT(atime), COUNT(unopened_until) FROM files "
            "WHERE type = 'file'").fetchone()

    print(f"entries   {files}  ({hashed} hashed, {stale} stale)")
    if regular:
        print(f"atime     {with_atime} of {regular} files have a trusted last-access "
              f"time ({with_atime / regular:.0%}); {unopened} known not opened since "
              f"they last changed")
    print(f"roots     {len(root_rows)}")
    # rc is NOT reset here: a permissions failure found above must survive to
    # the exit status, not be overwritten by a later clean check.
    trust = None
    if root_rows:
        measured, unmeasured = atime.measure(paths.data_dir())
        trust = atime.Trust(measured=measured, unmeasured=unmeasured)
    for r in root_rows:
        if r["source"] == roots.POLICY:
            # An imported listing describes another machine's filesystem; it is
            # not on this disk, and that is not a fault.
            print(f"  {r['path']}  imported listing ({r['host']}) "
                  f"{r['last_scan'] or 'never'}")
            setting = r["atime_setting"]
            mode = importer.atime_mode(setting) if setting else atime.NONE
            print(f"    access times: {_MODE_MEANS[mode]} "
                  f"({importer.atime_reason(setting)})")
            continue
        gone = "" if Path(r["path"]).is_dir() else "   <- gone from disk"
        if gone:
            rc = 1
        print(f"  {r['path']}  last scan {r['last_scan'] or 'never'}{gone}")
        if not gone:
            # Why a root has access times or not, so 0% is explained, not silent.
            mode, why = trust.check(r["path"], os.stat(r["path"]).st_dev)
            print(f"    access times: {_MODE_MEANS[mode]} ({why})")
    if stale:
        _err(f"cdm: {stale} hash(es) are stale; `cdm rescan --checksum` refreshes them")
    return rc


MCP_INSTALL_HINT = (
    "cdm mcp needs the optional MCP SDK. Add it with one of:\n"
    "  pipx inject ctrl-data-mgmt mcp\n"
    "  pip install 'ctrl-data-mgmt[mcp]'")


def _load_mcp_server():
    """Import the MCP adapter, which only exists with the [mcp] extra installed.

    Only an ImportError for the SDK itself means "extra not installed". Any other
    ImportError is a real bug and is re-raised rather than disguised as a
    missing dependency.
    """
    try:
        from . import mcp_server
    except ImportError as exc:
        if (exc.name or "").split(".")[0] == "mcp":
            return None
        raise
    return mcp_server


def cmd_mcp(args) -> int:
    # Nothing in this path may print to stdout: over stdio, stdout is the
    # protocol channel, and one stray line corrupts it for the client.
    if sys.version_info < (3, 10):
        _err(f"cdm mcp needs Python 3.10 or newer (the MCP SDK's minimum); "
             f"this is {sys.version.split()[0]}. The rest of cdm works on 3.9.")
        return 2
    server = _load_mcp_server()
    if server is None:
        _err(MCP_INSTALL_HINT)
        return 2
    try:
        return server.serve(expose_names=args.expose_names)
    except db.IndexUnavailable as exc:
        _err(f"cdm mcp: {exc}")
        return 1


def _threads_for(args, root: Path) -> int:
    """Explicit --threads wins; otherwise measure the filesystem and decide."""
    if args.threads:
        return max(1, args.threads)
    result = probe.probe(root)
    _err(f"  {result.describe()}")
    return result.threads


def _add_scan_flags(p) -> None:
    p.add_argument("-j", "--threads", type=int, metavar="N",
                   help="walker threads; default measures the filesystem "
                        "(1 if local, up to 8 if remote)")
    p.add_argument("--restart", action="store_true",
                   help="ignore any checkpoint and scan from the beginning")
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
    p.add_argument("--progress", nargs="?", type=float, const=10.0, metavar="SECS",
                   help="log a timestamped rate line every SECS seconds (default "
                        "10) even when stderr is not a terminal")


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
    f.add_argument("--iname", metavar="GLOB",
                   help="like --name but case-insensitive, which is what you "
                        "usually want on macOS and Windows filesystems")
    f.add_argument("--type", choices=["file", "dir", "link"])
    f.add_argument("--larger-than", metavar="SIZE")
    f.add_argument("--smaller-than", metavar="SIZE")
    f.add_argument("--modified-after", metavar="WHEN", help="7d, 24h, or 2026-08-01")
    f.add_argument("--modified-before", metavar="WHEN")
    f.add_argument("--accessed-after", metavar="WHEN",
                   help="last read after WHEN; files with no trusted access time never match")
    f.add_argument("--accessed-before", metavar="WHEN")
    f.add_argument("--fileset", metavar="NAME",
                   help="Storage Scale fileset (imported listings only)")
    f.add_argument("--pool", metavar="NAME",
                   help="Storage Scale storage pool (imported listings only)")
    f.add_argument("--unopened", action="store_true",
                   help="only files known not to have been opened since they last changed")
    f.add_argument("--root", metavar="PATH", help="restrict to one root (and roots nested in it)")
    f.add_argument("--order", choices=["size", "mtime", "atime", "name", "path"],
                   default="size")
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

    w = sub.add_parser("guide", help="step-by-step: what to do next, from first scan "
                                     "to a well-kept index")
    w.add_argument("--all", action="store_true", help="explain every step, not just the next")
    w.add_argument("--schedule", action="store_true",
                   help="print a nightly rescan job to install (launchd or cron)")
    w.add_argument("--json", action="store_true")
    w.set_defaults(func=cmd_guide)

    i = sub.add_parser(
        "import", help="load a Storage Scale policy listing into the index",
        description="Load a listing written by the script `cdm policy` prints. It "
                    "must be complete -- header and end marker intact -- or nothing "
                    "is imported.")
    i.add_argument("--policy", required=True, metavar="FILE.raw",
                   help="the listing the `cdm policy` script wrote")
    i.add_argument("--root", metavar="PATH",
                   help="the root to record it under (default: the directory "
                        "every listed path shares, e.g. the fileset's junction)")
    i.add_argument("--progress", nargs="?", type=float, const=10.0, metavar="SECS",
                   help="log a timestamped rate line every SECS seconds (default 10) "
                        "even when stderr is not a terminal")
    i.set_defaults(func=cmd_import)

    h = sub.add_parser(
        "hash", help="hash files that could be duplicates (same size as another)",
        description="Hash indexed files that share a size with another indexed file "
                    "and have no valid hash -- typically an imported Storage Scale "
                    "listing. Run it where the files are mounted. Resumable.")
    h.add_argument("--root", metavar="PATH", help="only files under PATH")
    h.add_argument("--fileset", metavar="NAME", help="only this Storage Scale fileset")
    h.add_argument("--full", action="store_true",
                   help="hash every byte (integrity) instead of both ends (dedupe)")
    h.add_argument("--min-size", metavar="SIZE", default="1",
                   help="skip files smaller than SIZE")
    h.add_argument("--progress", nargs="?", type=float, const=10.0, metavar="SECS",
                   help="log a timestamped rate line every SECS seconds (default 10) "
                        "even when stderr is not a terminal")
    h.set_defaults(func=cmd_hash)

    st = sub.add_parser(
        "storage", help="files and bytes by Storage Scale pool and fileset",
        description="Totals by storage pool and fileset, from imported Storage Scale "
                    "listings.")
    st.add_argument("--root", metavar="PATH",
                    help="restrict to one root (and roots nested in it)")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_storage)

    o = sub.add_parser(
        "policy", help="print a script that lists a Storage Scale fileset for import",
        description="Print a script for an administrator to run as root: an "
                    "mmapplypolicy LIST rule (-I defer, reads metadata only) whose "
                    "output `cdm import --policy` reads. cdm never runs it.")
    o.add_argument("--device", required=True, metavar="FS",
                   help="the Storage Scale filesystem device name")
    scope = o.add_mutually_exclusive_group(required=True)
    scope.add_argument("--fileset", metavar="NAME", help="list this fileset only")
    scope.add_argument("--whole-filesystem", action="store_true",
                       help="list every fileset: the index will hold every user's "
                            "file names")
    o.add_argument("--output", metavar="FILE.raw",
                   help="where the script writes the listing (default "
                        "DEVICE-FILESET.list.raw)")
    o.add_argument("--nodes", metavar="NODES",
                   help="passed to mmapplypolicy -N: nodes or node classes that "
                        "share the scan")
    o.set_defaults(func=cmd_policy)

    s = sub.add_parser("suggest", help="ranked things worth doing, with the command "
                                       "for each (changes nothing)")
    s.add_argument("--root", metavar="PATH",
                   help="restrict to one root (and roots nested in it)")
    s.add_argument("--older-than", metavar="DAYS", type=_days, default=90,
                   help="how long build output must be untouched (default 90d)")
    s.add_argument("--fast-pool", metavar="POOL", default=suggest.FAST_POOL,
                   help="the Storage Scale pool to keep for active data "
                        f"(default {suggest.FAST_POOL})")
    s.add_argument("--cold-pool", metavar="POOL",
                   help="where cold data should go (default: the one other pool "
                        "the data uses, if there is exactly one)")
    s.add_argument("--cold-after", metavar="DAYS", type=_days, default=suggest.COLD_DAYS,
                   help="neither modified nor read for this long counts as cold "
                        f"(default {suggest.COLD_DAYS}d)")
    s.add_argument("--limit", type=int, default=20, help="suggestions to show")
    s.add_argument("--items", type=int, default=5, help="paths per suggestion")
    s.add_argument("--all", action="store_true", help="every path per suggestion")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_suggest)

    g = sub.add_parser("forget", help="drop a root and its rows from the index")
    g.add_argument("path")
    g.set_defaults(func=cmd_forget)

    t = sub.add_parser("stat", help="what the index knows about one path")
    t.add_argument("path")
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_stat)

    sub.add_parser("doctor", help="index health").set_defaults(func=cmd_doctor)

    m = sub.add_parser(
        "mcp", help="serve the index to an AI client over MCP (read-only, stdio)",
        description="Serve the index to an MCP client such as Claude Code or Claude "
                    "Desktop. Read-only. By default only totals, histograms and "
                    "extensions are available -- no file or directory names.")
    m.add_argument("--expose-names", action="store_true",
                   help="also offer find, du, dupes and stat, which return paths; "
                        "those paths are sent to the model client")
    m.set_defaults(func=cmd_mcp)
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
