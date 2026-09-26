"""What a model client may ask, and what it may be told.

This is the whole behaviour of `cdm mcp`, kept free of the MCP SDK so it can be
tested on every supported Python -- the SDK needs 3.10+, this project supports
3.9. mcp_server.py is only an adapter that registers these methods.

Three rules, each enforced structurally rather than by care:

* READ-ONLY. Every call opens the index with SQLite mode=ro, so a write is
  refused by SQLite itself. The server never walks directories and never reads
  file contents -- which is why `dupes --verify`, which re-reads files, has no
  tool here.

* NAMES ARE OPT-IN. The shape tools return no path below a root. The name tools
  are not filtered versions of anything: they are simply not registered unless
  the server was started with --expose-names, so a model cannot call what it
  cannot see. See docs/adr/0002.

* FRESH PER CALL. A new read-only connection per call: nothing is cached across
  calls, a `cdm rescan` in another terminal is visible to the next question,
  and no connection ever crosses a thread boundary inside the SDK.
"""
from __future__ import annotations

import os
import stat as stat_mod
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db, paths, query, shape
from . import suggest as suggest_mod

MAX_ROWS = 500

SHAPE_TOOLS = {
    "summary": "Per-root totals (files, directories, bytes) and how many days "
               "since each root was last scanned. Start here. A root nested in "
               "another has `inside` set; each file is counted once, under its "
               "most specific root, so per-root figures add up to the total.",
    "size_histogram": "How many files, and how many bytes, fall in each size "
                      "range. Answers 'is the space in a few huge files or many "
                      "small ones'.",
    "age_histogram": "How many files, and how many bytes, by time since last "
                     "modification. Answers 'how much of this is cold'.",
    "extensions": "Top file extensions by bytes. Answers 'what kind of data is "
                  "taking the space'. Totals always add up to the whole.",
    "duplicates_summary": "How much space looks duplicated, how much is "
                          "confirmed, and how much of the tree was hashed at "
                          "all. Returns no filenames.",
    "suggest": "Ranked things worth doing -- caches, stale build output, old "
               "installers, large git histories, model files, duplicates, index "
               "housekeeping -- each with its size, why, a risk level (safe, "
               "review, none) and the command to run. Advisory: show the "
               "commands to the user; never run them on their behalf. Use this "
               "for 'what should I do' and 'what can I clean up'.",
}

NAME_TOOLS = {
    "find": "Search for files by name, size and modification time. Returns "
            "paths. Sizes like 100M or 2.5G; times like 7d, 24h or 2026-08-01.",
    "du": "Disk usage by subdirectory under a path, answered from the index. "
          "Returns directory paths.",
    "dupes": "Groups of files that share a hash. Returns paths. Partial-hash "
             "groups are probable, not confirmed.",
    "stat": "Everything the index records about one path.",
}


# Ready-made questions. Clients list these (Claude Code offers them as slash
# commands), so a user can start from a menu instead of guessing what to ask.
# (title, description, instructions without names, extra instructions with).
PROMPTS = {
    "disk-usage": (
        "What's using my disk?",
        "Where the space goes: roots, file sizes, file types and age.",
        "Call `summary`{scope}, then `size_histogram`, `extensions` and "
        "`age_histogram`. Explain in plain terms what is taking the space -- a "
        "few huge files or many small ones, what kind of data, how much has not "
        "changed in a year -- and finish with the top items from `suggest`.",
        " Use `du` on the largest roots to name the directories responsible.",
    ),
    "cleanup": (
        "What can I clean up?",
        "Ranked, advisory cleanup suggestions with the command for each.",
        "Call `suggest`{scope}. Present housekeeping (risk none) first, then "
        "`safe` items, then `review` items, each with its size, the reason and "
        "the exact command. Do not run any command yourself: the user decides "
        "and runs them. Mention that 'not modified' is judged by modification "
        "time, not last use, and that savings can overlap between suggestions.",
        " Include the paths each suggestion lists, and offer `du` for any of "
        "them the user wants to look into.",
    ),
    "duplicates": (
        "How much is duplicated?",
        "Duplicate totals, how complete they are, and where to act.",
        "Call `duplicates_summary`{scope}. Report candidate and confirmed "
        "totals, and say how much of the tree was hashed at all, because "
        "unhashed files cannot show up as duplicates. Then call `suggest` and "
        "say which duplicates would go away with a cache or dependency cleanup "
        "anyway. Anything deleted by hand should first be confirmed with "
        "`cdm dupes --verify` in a terminal.",
        " Use `dupes` to show the largest groups.",
    ),
    "recent-changes": (
        "What changed recently?",
        "How much data changed in the last week and month.",
        "Call `age_histogram`{scope} and describe how much changed in the last "
        "7 and 30 days. Check `summary` for when each root was last scanned: "
        "nothing after that is in the index.",
        " Use `find` with modified_after='7d', ordered by size, to name the "
        "biggest recent changes.",
    ),
    "index-health": (
        "Is the index up to date?",
        "Scan age and hash coverage, with what to run if either is lacking.",
        "Call `summary` and `duplicates_summary`. Report how long ago each root "
        "was scanned and what share of files is hashed. If a scan is over a week "
        "old suggest `cdm rescan`; if coverage is low suggest "
        "`cdm rescan --checksum`.",
        "",
    ),
}


def prompt_text(name: str, expose_names: bool, root: str | None = None) -> str:
    _, _, text, with_names = PROMPTS[name]
    scope = f" with root={root!r}" if root else ""
    return text.format(scope=scope) + (with_names if expose_names else "")


def _next(result: dict[str, Any], steps: list[str]) -> dict[str, Any]:
    """Attach follow-ups, so a client that only knows one tool finds the rest.

    Fixed strings naming tools and commands; never anything from the index.
    """
    result["next_steps"] = steps
    return result


def tool_names(expose_names: bool) -> list[str]:
    return list(SHAPE_TOOLS) + (list(NAME_TOOLS) if expose_names else [])


def _iso(epoch) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch).isoformat(
        timespec="seconds")


def _file_row(r) -> dict[str, Any]:
    return {"path": r["path"], "type": r["type"], "bytes": r["size"],
            "size": shape.human(r["size"]), "modified": _iso(r["mtime"])}


class Catalog:
    """The tools, bound to one index and one host."""

    def __init__(self, index: Path | None = None, host: str | None = None,
                 expose_names: bool = False):
        self.index = Path(index) if index else paths.index_path()
        self.host = host or paths.this_host()
        # Decides what `suggest` may put in its answer. The name TOOLS are
        # gated at registration (mcp_server.py); this is the one shape tool
        # whose detail depends on the same choice.
        self.expose_names = expose_names

    def _open(self):
        return closing(db.connect_readonly(self.index))

    def _root(self, conn, root: str | None) -> str | None:
        """Accept only a known root, verbatim.

        A root the model makes up is refused with the list of real ones. The
        refusal names only roots, which are already shape-level; it never
        confirms or denies anything beneath one.
        """
        if root is None:
            return None
        known = [r[0] for r in conn.execute(
            "SELECT path FROM roots WHERE host = ? ORDER BY path", (self.host,))]
        # Roots are stored resolved, so on macOS /tmp/x was recorded as
        # /private/tmp/x. Accept either spelling of a real root.
        spellings = (os.path.normpath(os.path.expanduser(root)),
                     str(Path(root).expanduser().resolve()))
        for candidate in spellings:
            if candidate in known:
                return candidate
        raise ValueError(f"not an indexed root: {root!r}. Known roots: {known}")

    # --- shape: returned by default ----------------------------------------

    def summary(self, root: str | None = None) -> dict[str, Any]:
        with self._open() as conn:
            out = shape.summary(conn, host=self.host, root=self._root(conn, root))
        steps = ["Call `suggest` for a ranked list of what is worth doing."]
        if any((r["last_scan_age_days"] or 0) >= suggest_mod.STALE_SCAN_DAYS
               for r in out["roots"]):
            steps.insert(0, "Some roots were scanned a week or more ago; answers "
                            "about them are that old. Suggest `cdm rescan`.")
        if self.expose_names:
            steps.append("Call `du` on the largest root to see where its space is.")
        return _next(out, steps)

    def size_histogram(self, root: str | None = None) -> dict[str, Any]:
        with self._open() as conn:
            out = shape.size_histogram(conn, host=self.host,
                                       root=self._root(conn, root))
        return _next(out, ["`extensions` shows what kind of data the space is.",
                           *(["`find` with larger_than lists the biggest files."]
                             if self.expose_names else [])])

    def age_histogram(self, root: str | None = None) -> dict[str, Any]:
        with self._open() as conn:
            out = shape.age_histogram(conn, host=self.host,
                                      root=self._root(conn, root))
        return _next(out, ["Ages are by last modification, not last use.",
                           "`suggest` flags stale build output, old installers "
                           "and caches."])

    def extensions(self, root: str | None = None, limit: int = 20) -> dict[str, Any]:
        with self._open() as conn:
            out = shape.extensions(conn, host=self.host, root=self._root(conn, root),
                                   limit=max(1, min(limit, 200)))
        return _next(out, ["`find` with name='*.<ext>' lists files of one type."]
                     if self.expose_names else
                     ["`suggest` turns this into concrete cleanup candidates."])

    def duplicates_summary(self, root: str | None = None) -> dict[str, Any]:
        with self._open() as conn:
            out = shape.duplicates_summary(conn, host=self.host,
                                           root=self._root(conn, root))
        cov = out["coverage"]
        steps = []
        if cov["files"] and (cov["partial_hashed"] + cov["full_hashed"]) < cov["files"] * 0.9:
            steps.append("Much of the tree is unhashed, so these totals are low. "
                         "Suggest `cdm rescan --checksum`.")
        steps.append("`dupes` lists the groups." if self.expose_names else
                     "`suggest` says where duplicates are worth acting on.")
        steps.append("Partial-hash groups are probable; `cdm dupes --verify` in a "
                     "terminal confirms them before anything is deleted.")
        return _next(out, steps)

    def suggest(self, root: str | None = None, older_than_days: int = 90,
                limit: int = 20) -> dict[str, Any]:
        with self._open() as conn:
            out = suggest_mod.suggest(
                conn, host=self.host, root=self._root(conn, root),
                names=self.expose_names, older_than_days=max(0, older_than_days),
                limit=max(1, min(limit, 100)))
        steps = ["Present housekeeping (risk none) first, then safe, then review. "
                 "Show each command; the user runs it, you do not."]
        if not self.expose_names:
            steps.append("Paths are not shared by this server. For them, the user "
                         "can run `cdm suggest` in a terminal.")
        return _next(out, steps)

    # --- names: only registered with --expose-names --------------------------

    def find(self, name: str | None = None, iname: str | None = None,
             kind: str | None = None, larger_than: str | None = None,
             smaller_than: str | None = None, modified_after: str | None = None,
             modified_before: str | None = None, root: str | None = None,
             order: str = "size", limit: int = 50) -> dict[str, Any]:
        if kind not in (None, "file", "dir", "link"):
            raise ValueError("kind must be file, dir or link")
        if order not in ("size", "mtime", "name", "path"):
            raise ValueError("order must be size, mtime, name or path")
        limit = max(1, min(limit, MAX_ROWS))
        with self._open() as conn:
            rows = query.find(
                conn, host=self.host, root=self._root(conn, root), name=name,
                iname=iname, kind=kind,
                larger_than=query.parse_size(larger_than) if larger_than else None,
                smaller_than=query.parse_size(smaller_than) if smaller_than else None,
                modified_after=(query.parse_when(modified_after)
                                if modified_after else None),
                modified_before=(query.parse_when(modified_before)
                                 if modified_before else None),
                order=order, limit=limit + 1)
        return _next({"results": [_file_row(r) for r in rows[:limit]],
                      "truncated": len(rows) > limit},
                     ["`stat` shows everything recorded about one result.",
                      "`du` on a result's directory shows what else is there."])

    def du(self, path: str, depth: int = 1, limit: int = 40) -> dict[str, Any]:
        limit = max(1, min(limit, MAX_ROWS))
        with self._open() as conn:
            rows = query.disk_usage(conn, path, depth=max(1, depth),
                                    host=self.host, limit=limit)
        return _next({"results": [{"path": r["path"], "files": r["files"],
                                   "bytes": r["bytes"], "size": shape.human(r["bytes"])}
                                  for r in rows]},
                     ["Call `du` on the largest result to drill down.",
                      "`suggest` says which of this is safe to remove."])

    def dupes(self, min_size: str = "1", limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(limit, MAX_ROWS))
        with self._open() as conn:
            groups = query.dupe_groups(conn, host=self.host,
                                       min_size=query.parse_size(min_size),
                                       limit=limit)
        return _next({"groups": [{
            "confirmed": g["hash_kind"] == "full",
            "bytes_each": g["size"], "size_each": shape.human(g["size"]),
            "reclaimable_bytes": g["reclaimable"],
            "paths": [m["path"] for m in g["members"]],
        } for g in groups]}, ["Unconfirmed groups are probable, not certain: "
                              "`cdm dupes --verify` in a terminal confirms them."])

    def stat(self, path: str) -> dict[str, Any]:
        with self._open() as conn:
            row = query.stat_one(conn, path, host=self.host)
        if row is None:
            return {"found": False, "path": path}
        return {"found": True, **_file_row(row), "root": row["root"],
                "created": _iso(row["ctime"]), "hash": row["hash"],
                "hash_kind": row["hash_kind"],
                "hash_stale": query.hash_is_stale(row) if row["hash"] else None,
                "last_seen": row["seen_at"]}

    # --- the startup report ---------------------------------------------------

    def banner(self, expose_names: bool) -> list[str]:
        """What is being served, printed to stderr when the server starts.

        stderr because over stdio, stdout IS the protocol channel.
        """
        lines = [f"cdm mcp: serving {self.index} (read-only) for host {self.host}"]
        with self._open() as conn:
            roots = conn.execute(
                "SELECT path, last_scan FROM roots WHERE host = ? ORDER BY path",
                (self.host,)).fetchall()
        if not roots:
            lines.append("cdm mcp: WARNING the index has no roots for this host; "
                         "every answer will be empty")
        for r in roots:
            age = ""
            if r["last_scan"]:
                days = (time.time() - datetime.fromisoformat(
                    r["last_scan"]).timestamp()) / 86400
                age = f" (last scan {days:.1f} days ago)"
            lines.append(f"cdm mcp:   root {r['path']}{age}")
        lines.append(f"cdm mcp: tools: {', '.join(tool_names(expose_names))}")
        if expose_names:
            lines.append("cdm mcp: WARNING --expose-names: file and directory names "
                         "will be sent to the model client")
        else:
            lines.append("cdm mcp: names are NOT exposed; only totals, histograms "
                         "and extensions (start with --expose-names to allow)")
        lines.extend(self.permission_warnings())
        return lines

    def permission_warnings(self) -> list[str]:
        """Check modes without changing them: a read-only server modifies nothing."""
        out = []
        index = self.index
        for p in (index, index.with_name(index.name + "-wal"),
                  index.with_name(index.name + "-shm"), index.parent):
            try:
                mode = stat_mod.S_IMODE(os.stat(p).st_mode)
            except OSError:
                continue
            if mode & paths.OTHERS_MASK:
                out.append(f"cdm mcp: WARNING {p} is mode {oct(mode)} and readable "
                           f"by other users; run `cdm doctor`")
        return out
