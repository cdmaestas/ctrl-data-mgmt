"""Suggestions: what the index says is worth doing, ranked, with the command.

One engine, many front-ends. `cdm suggest`, the MCP `suggest` tool and any
later UI all render the output of suggest() and none of them decides anything
on its own -- see docs/adr/0003. Rules that are not negotiable:

* READ-ONLY, AND ADVISORY. Every suggestion carries a command for a person to
  run. Nothing here deletes, and no front-end may run an action on its own.
* HONEST ABOUT RISK. Each suggestion says whether its action is `safe` (the
  data regenerates by itself: a package cache), `review` (look first: build
  output, installers, models, duplicates) or `none` (index housekeeping).
* HONEST ABOUT EVIDENCE. Staleness is measured from mtime, because that is what
  the index records. mtime says when something was last CHANGED, not last used:
  a model loaded every day but never rewritten looks cold. Nothing judged by
  mtime is ever marked `safe` for that reason alone.
* NAMES FOLLOW THE CALLER. With names=False the output carries no path below a
  root, the same contract as shape.py (docs/adr/0002). Rule labels and commands
  come from the fixed vocabulary in this file, never from the index.

Stdlib only, like shape.py, so every front-end can use it on every Python.
"""
from __future__ import annotations

import shlex
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import roots as roots_mod
from .shape import DAY, GB, MB, human

SAFE, REVIEW, NONE = "safe", "review", "none"

STALE_SCAN_DAYS = 7
MIN_HASH_COVERAGE = 0.9
MIN_KNOWN_CACHE = 100 * MB
MIN_GENERIC_CACHE = GB
MIN_GIT = GB
MIN_INSTALLER = 100 * MB
INSTALLER_AGE_DAYS = 30
MIN_MODEL_FILE = 500 * MB
MIN_DUPLICATES = 100 * MB
MAX_ITEMS = 10

# (path suffix, label, command, risk). The command may use {path}. Specific
# entries beat generic ones: bytes already claimed by a more specific cache are
# not counted again in an enclosing one.
KNOWN_CACHES = (
    ((".npm", "_cacache"), "npm cache", "npm cache clean --force", SAFE),
    ((".cache", "uv"), "uv cache", "uv cache clean", SAFE),
    ((".cache", "pip"), "pip cache", "pip cache purge", SAFE),
    (("Library", "Caches", "pip"), "pip cache", "pip cache purge", SAFE),
    ((".cache", "yarn"), "Yarn cache", "yarn cache clean", SAFE),
    (("Library", "Caches", "Yarn"), "Yarn cache", "yarn cache clean", SAFE),
    ((".yarn", "berry", "cache"), "Yarn cache", "yarn cache clean --all", SAFE),
    (("Library", "Caches", "Homebrew"), "Homebrew downloads",
     "brew cleanup --prune=all", SAFE),
    (("Library", "Developer", "Xcode", "DerivedData"), "Xcode build products",
     "rm -rf {path}", SAFE),
    (("go", "pkg", "mod"), "Go module cache", "go clean -modcache", REVIEW),
    ((".gradle", "caches"), "Gradle cache", "rm -rf {path}", REVIEW),
    ((".m2", "repository"), "Maven repository", "rm -rf {path}", REVIEW),
    ((".cargo", "registry"), "Cargo registry", "rm -rf {path}", REVIEW),
    ((".cache", "huggingface"), "Hugging Face downloads",
     "huggingface-cli delete-cache", REVIEW),
    (("Library", "Caches", "ms-playwright"), "Playwright browsers",
     "npx playwright uninstall --all", REVIEW),
    (("Library", "Caches"), "macOS application caches",
     "quit the apps, then remove what you no longer need under {path}", REVIEW),
)
GENERIC_CACHE_NAMES = frozenset({"cache", "caches", ".cache"})

# Directory name -> (marker files, any of which proves it is generated, and
# which side they sit on), label, command, risk.
SIBLING, INSIDE = "sibling", "inside"
REBUILDABLE = (
    ("node_modules", (SIBLING, ("package.json",)), "npm dependencies",
     "rm -rf {path}  # npm install to restore", SAFE),
    (".venv", (INSIDE, ("pyvenv.cfg",)), "Python virtualenv",
     "rm -rf {path}  # recreate with uv sync or python -m venv", SAFE),
    ("venv", (INSIDE, ("pyvenv.cfg",)), "Python virtualenv",
     "rm -rf {path}  # recreate with uv sync or python -m venv", SAFE),
    (".tox", (SIBLING, ("tox.ini", "pyproject.toml", "setup.cfg")), "tox environments",
     "rm -rf {path}", SAFE),
    ("target", (SIBLING, ("Cargo.toml",)), "Rust build output",
     "cargo clean --manifest-path {parent}/Cargo.toml", SAFE),
)
_PROJECT = ("package.json", "pyproject.toml", "setup.py", "Makefile", "CMakeLists.txt")
_REBUILDABLE_NAMES = frozenset(r[0] for r in REBUILDABLE)
BUILD_OUTPUT = frozenset({"dist", "build", "out", "dist-electron", ".next", ".nuxt",
                          ".parcel-cache", ".turbo"})

INSTALLER_EXT = frozenset({"dmg", "pkg", "iso", "xip", "msi", "exe", "appimage",
                           "deb", "rpm"})
ARCHIVE_EXT = frozenset({"zip", "tgz", "7z", "gz", "xz", "bz2"})
DOWNLOAD_DIRS = frozenset({"Downloads", "Desktop"})

MODEL_EXT = frozenset({"gguf", "safetensors", "ckpt", "pt", "pth", "onnx", "mlmodel",
                       "mlpackage", "bin", "npz"})
MODEL_DIRS = frozenset({"models", "blobs", "hub"})


@dataclass
class Item:
    path: str
    bytes: int
    files: int = 0
    newest: str | None = None
    action: str | None = None

    @property
    def size(self) -> str:
        return human(self.bytes)


@dataclass
class Suggestion:
    id: str
    title: str
    detail: str
    risk: str
    bytes: int = 0
    action: str | None = None
    items: list[Item] = field(default_factory=list)

    def render(self, names: bool, max_items: int = MAX_ITEMS) -> dict:
        out = {"id": self.id, "title": self.title, "detail": self.detail,
               "risk": self.risk, "bytes": self.bytes, "size": human(self.bytes),
               "action": self.action, "item_count": len(self.items)}
        if names:
            out["items"] = [{**asdict(i), "size": i.size} for i in self.items[:max_items]]
        elif self.items and self.action is None:
            out["action"] = ("run `cdm suggest` in a terminal to see which "
                             "paths, and the command for each")
        return out


class _Index:
    """Queries the rules share, scoped to one host and optionally one root."""

    def __init__(self, conn, host: str, root: str | None):
        self.conn, self.host, self.root = conn, host, root
        self.scope, self.params = "host = ?", [host]
        if root is not None:
            self.scope += " AND " + roots_mod.scope_sql()
            self.params += roots_mod.scope_params(root)

    def dirs(self):
        return self.conn.execute(
            f"SELECT path, name, parent FROM files WHERE type = 'dir' AND {self.scope}",
            self.params).fetchall()

    def exists(self, path: str) -> bool:
        return self.conn.execute("SELECT 1 FROM files WHERE host = ? AND path = ?",
                                 (self.host, path)).fetchone() is not None

    def under(self, directory: str) -> tuple[int, int, float | None]:
        """(files, bytes, newest mtime) below a directory, from the primary key."""
        lo, hi = roots_mod.bounds(directory)
        r = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0), MAX(mtime) FROM files "
            "WHERE host = ? AND path >= ? AND path < ? AND type = 'file'",
            (self.host, lo, hi)).fetchone()
        return r[0], r[1], r[2]


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch).isoformat(
        timespec="seconds")


def _topmost(paths):
    """Drop every path that lies below another path in the set."""
    kept: list[str] = []
    for p in sorted(paths, key=len):
        if not any(p.startswith(roots_mod.bounds(k)[0]) for k in kept):
            kept.append(p)
    return kept


# --- housekeeping -------------------------------------------------------------

def _index_rules(idx: _Index, now: float) -> list[Suggestion]:
    out = []
    rwhere, rparams = "host = ?", [idx.host]
    if idx.root is not None:
        rwhere += " AND " + roots_mod.scope_sql("path")
        rparams += roots_mod.scope_params(idx.root)
    for r in idx.conn.execute(
            f"SELECT path, last_scan FROM roots WHERE {rwhere} ORDER BY path", rparams):
        if r["last_scan"] is None:
            continue
        age = (now - datetime.fromisoformat(r["last_scan"]).timestamp()) / DAY
        if age >= STALE_SCAN_DAYS:
            out.append(Suggestion(
                f"index.stale:{r['path']}", f"Rescan {r['path']}",
                f"Last scanned {age:.0f} days ago; every answer about it is that old.",
                NONE, action=f"cdm rescan {r['path']}"))
        files, hashed, stale = idx.conn.execute(
            "SELECT COUNT(*), COUNT(hash), "
            "       SUM(hash IS NOT NULL AND (hash_size != size OR hash_mtime != mtime)) "
            "FROM files WHERE host = ? AND root = ? AND type = 'file'",
            (idx.host, r["path"])).fetchone()
        if files and hashed / files < MIN_HASH_COVERAGE:
            out.append(Suggestion(
                f"index.unhashed:{r['path']}", f"Checksum {r['path']}",
                f"Only {hashed:,} of {files:,} files are hashed, so duplicate "
                f"findings under it are incomplete.",
                NONE, action=f"cdm rescan --checksum {r['path']}"))
        elif stale:
            out.append(Suggestion(
                f"index.stale-hashes:{r['path']}", f"Refresh hashes under {r['path']}",
                f"{stale:,} files changed since they were hashed.",
                NONE, action=f"cdm rescan --checksum {r['path']}"))
    return out


# --- caches -------------------------------------------------------------------

def _cache_rules(idx: _Index, dirs) -> list[Suggestion]:
    hits: dict[str, tuple] = {}
    for d in dirs:
        path = d["path"]
        parts = path.split("/")
        for suffix, label, command, risk in KNOWN_CACHES:
            # A sandboxed app's or system daemon's Library/Caches is managed
            # by macOS; "quit the app and clean it" is not advice for those.
            if suffix == ("Library", "Caches") and (
                    "Containers" in parts or "Group Containers" in parts):
                continue
            if tuple(parts[-len(suffix):]) == suffix:
                hits.setdefault(path, (label, command, risk, MIN_KNOWN_CACHE))
                break
        else:
            if d["name"].lower() in GENERIC_CACHE_NAMES:
                hits[path] = ("Cache directory", "rm -rf {path}", REVIEW,
                              MIN_GENERIC_CACHE)

    # Most specific first, so an enclosing cache is credited only with what is
    # not already listed under it.
    claimed: list[tuple[str, int]] = []
    groups: dict[tuple, Suggestion] = {}
    for path in sorted(hits, key=len, reverse=True):
        label, command, risk, minimum = hits[path]
        files, total, newest = idx.under(path)
        inner = sum(b for p, b in claimed if p.startswith(roots_mod.bounds(path)[0]))
        claimed.append((path, total))
        net = total - inner
        if net < minimum:
            continue
        key = (label, risk)
        s = groups.get(key)
        if s is None:
            generic = label == "Cache directory"
            if risk == SAFE:
                why = "Rebuilt automatically when needed."
            elif generic or "{path}" in command and "quit" in command:
                why = ("Usually regenerated, but some apps keep state in caches; "
                       "look before deleting.")
            else:
                why = ("Downloaded again when needed, which costs time and "
                       "bandwidth; worth it if you no longer use the tool.")
            s = groups[key] = Suggestion(
                f"cache:{label}", f"Clear {label}" if not generic
                else "Large cache directories", why,
                risk, action=None if "{path}" in command else command)
        s.bytes += net
        s.items.append(Item(path, net, files, _iso(newest),
                            command.format(path=shlex.quote(path))))
    return list(groups.values())


# --- rebuildable dependencies and build output ---------------------------------

def _build_rules(idx: _Index, dirs, now: float, older_than_days: int) -> list[Suggestion]:
    cutoff = now - older_than_days * DAY
    # Only inside a git working tree, where "rebuild it" is actually true. An
    # installed VS Code extension also has package.json beside node_modules and
    # dist, and nothing will ever rebuild it.
    checkouts = {d["parent"] for d in dirs if d["name"] == ".git"}

    def in_checkout(path: str) -> bool:
        parts = path.split("/")
        return any("/".join(parts[:i]) in checkouts for i in range(len(parts) - 1, 1, -1))

    candidates: dict[str, tuple] = {}
    for d in dirs:
        name, path, parent = d["name"], d["path"], d["parent"]
        if (name not in BUILD_OUTPUT and name not in _REBUILDABLE_NAMES) \
                or not in_checkout(path):
            continue
        for dname, (where, markers), label, command, risk in REBUILDABLE:
            if name != dname:
                continue
            base = parent if where == SIBLING else path
            if any(idx.exists(f"{base}/{m}") for m in markers):
                candidates[path] = (label, command, risk)
        if name in BUILD_OUTPUT and any(idx.exists(f"{parent}/{m}") for m in _PROJECT):
            candidates.setdefault(path, ("Build output", "rm -rf {path}", REVIEW))

    by_risk: dict[str, Suggestion] = {}
    for path in _topmost(candidates):
        label, command, risk = candidates[path]
        files, total, newest = idx.under(path)
        if not total or (newest is not None and newest >= cutoff):
            continue
        s = by_risk.get(risk)
        if s is None:
            s = by_risk[risk] = Suggestion(
                "build:rebuildable" if risk == SAFE else "build:output",
                ("Dependencies you can reinstall" if risk == SAFE else
                 "Build output"),
                (f"Inside git checkouts, nothing modified in {older_than_days}+ "
                 f"days, and each is recreated by its project's install or build "
                 f"command."
                 if risk == SAFE else
                 f"Inside git checkouts, nothing modified in {older_than_days}+ "
                 f"days. Usually rebuildable, but may hold a release you meant "
                 f"to keep."),
                risk)
        s.bytes += total
        parent = path.rsplit("/", 1)[0]
        s.items.append(Item(path, total, files, _iso(newest),
                            command.format(path=shlex.quote(path),
                                           parent=shlex.quote(parent))))
    return list(by_risk.values())


def _git_rule(idx: _Index, dirs) -> list[Suggestion]:
    s = Suggestion("git:large", "Large git histories",
                   "Repacking usually shrinks history; a shallow re-clone "
                   "shrinks it most if you do not need the history locally.",
                   REVIEW)
    for d in dirs:
        if d["name"] != ".git":
            continue
        files, total, newest = idx.under(d["path"])
        if total >= MIN_GIT:
            s.bytes += total
            s.items.append(Item(d["path"], total, files, _iso(newest),
                                f"git -C {shlex.quote(d['parent'])} gc "
                                f"--aggressive --prune=now"))
    return [s] if s.items else []


# --- single large files -------------------------------------------------------

def _ext(name: str) -> str:
    lowered = name.lower()
    return lowered.rsplit(".", 1)[-1] if "." in lowered.lstrip(".") else ""


def _file_rules(idx: _Index, now: float) -> list[Suggestion]:
    installers = Suggestion(
        "files:installers", "Old installers and downloaded archives",
        f"Installers and archives over {human(MIN_INSTALLER)} not modified in "
        f"{INSTALLER_AGE_DAYS}+ days. Usually downloadable again.", REVIEW)
    stores: dict[str, list] = {}
    for r in idx.conn.execute(
            f"SELECT path, name, parent, size, mtime FROM files "
            f"WHERE type = 'file' AND size >= ? AND {idx.scope}",
            [min(MIN_INSTALLER, MIN_MODEL_FILE), *idx.params]):
        ext = _ext(r["name"])
        parts = r["path"].split("/")
        old = r["mtime"] < now - INSTALLER_AGE_DAYS * DAY
        if r["size"] >= MIN_INSTALLER and old and (
                ext in INSTALLER_EXT
                or (ext in ARCHIVE_EXT and DOWNLOAD_DIRS & set(parts[:-1]))):
            installers.bytes += r["size"]
            installers.items.append(Item(r["path"], r["size"], 1, _iso(r["mtime"]),
                                         f"rm {shlex.quote(r['path'])}"))
            continue
        if r["size"] >= MIN_MODEL_FILE and (ext in MODEL_EXT
                                            or MODEL_DIRS & set(parts[:-1])):
            # Group by the model store: the nearest enclosing `models` directory,
            # else the file's own directory.
            store = r["parent"]
            for i in range(len(parts) - 2, 0, -1):
                if parts[i] == "models":
                    store = "/".join(parts[:i + 1])
                    break
            entry = stores.setdefault(store, [0, 0, 0.0])
            entry[0] += r["size"]
            entry[1] += 1
            entry[2] = max(entry[2], r["mtime"])

    out = []
    if installers.items:
        installers.items.sort(key=lambda i: i.bytes, reverse=True)
        out.append(installers)
    if stores:
        models = Suggestion(
            "files:models", "AI model files",
            "Large model weights, grouped by where they are stored. Dates are "
            "last MODIFIED, not last used: a model you run daily looks just as "
            "old. Remove models with the tool that downloaded them.", REVIEW,
            action="e.g. `ollama list` then `ollama rm <model>`")
        for store, (total, n, newest) in stores.items():
            models.bytes += total
            models.items.append(Item(store, total, n, _iso(newest)))
        models.items.sort(key=lambda i: i.bytes, reverse=True)
        out.append(models)
    return out


def _duplicate_rule(idx: _Index, names: bool) -> list[Suggestion]:
    r = idx.conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(size * (n - 1)), 0) FROM ("
        f"  SELECT size, COUNT(*) AS n FROM files "
        f"  WHERE hash IS NOT NULL AND size >= 1 AND {idx.scope} "
        f"  GROUP BY hash_kind, hash, size HAVING n > 1)", idx.params).fetchone()
    groups, reclaimable = r[0], r[1]
    if reclaimable < MIN_DUPLICATES:
        return []
    s = Suggestion(
        "dupes", "Duplicate files",
        f"{groups:,} groups of files with identical hashes. Much of this is "
        f"usually inside caches and dependency folders above; removing those "
        f"removes these too. Confirm before deleting anything by hand.",
        REVIEW, bytes=reclaimable, action="cdm dupes --min-size 10M --verify")
    if names:
        for g in idx.conn.execute(
                f"SELECT hash, hash_kind, size, COUNT(*) AS n FROM files "
                f"WHERE hash IS NOT NULL AND size >= ? AND {idx.scope} "
                f"GROUP BY hash_kind, hash, size HAVING n > 1 "
                f"ORDER BY size * (n - 1) DESC LIMIT ?",
                [MB, *idx.params, MAX_ITEMS]):
            first = idx.conn.execute(
                f"SELECT path FROM files WHERE hash = ? AND hash_kind = ? AND size = ? "
                f"AND {idx.scope} ORDER BY path LIMIT 1",
                [g["hash"], g["hash_kind"], g["size"], *idx.params]).fetchone()[0]
            s.items.append(Item(first, g["size"] * (g["n"] - 1), g["n"]))
    return [s]


# --- entry point ----------------------------------------------------------------

def suggest(conn, *, host: str, root: str | None = None, names: bool = True,
            older_than_days: int = 90, limit: int = 20,
            max_items: int = MAX_ITEMS, now: float | None = None) -> dict:
    """Ranked suggestions for everything indexed on `host` (or under `root`)."""
    now = time.time() if now is None else now
    idx = _Index(conn, host, root)
    dirs = idx.dirs()
    found = (_index_rules(idx, now) + _cache_rules(idx, dirs)
             + _build_rules(idx, dirs, now, older_than_days) + _git_rule(idx, dirs)
             + _file_rules(idx, now) + _duplicate_rule(idx, names))
    # Housekeeping first, because it decides whether every other answer is
    # right; then the biggest savings.
    found.sort(key=lambda s: (s.risk != NONE, -s.bytes))
    for s in found:
        s.items.sort(key=lambda i: i.bytes, reverse=True)
    return {
        "suggestions": [s.render(names, max_items) for s in found[:max(1, limit)]],
        "truncated": len(found) > limit,
        "measured_from": "mtime",
        "note": ("Advisory only: nothing has been changed. Savings can overlap "
                 "between suggestions (a duplicate inside a cache is counted in "
                 "both). 'Not modified' is judged by mtime, not last access."),
    }
