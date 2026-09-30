#!/usr/bin/env python3
"""Turn a raw `cdm policy` listing into a test fixture that names nothing real.

Run it where the raw listing is -- on the cluster -- and copy only its output
off, so the raw listing never leaves. See tests/fixtures/README.md.

    sanitize_listing.py RAW.list.raw OUT.list

What changes, and what is kept:

* Every path component becomes a consistent placeholder by kind -- d0007 for a
  directory, f0412 for a file, l0003 for a symlink -- so the tree keeps its
  shape. A leading dot (hidden) and a common extension (.txt, .gguf, ...) are
  kept, because code under test cares about both. The same name maps to the
  same placeholder everywhere, as real trees repeat names.
* UIDs and GIDs are renumbered from 1000 in order of appearance; 0 stays 0.
* The cluster, filesystem, fileset and pool names become cluster1.example, fs1,
  fileset1..., and system / data1...; `system` is Storage Scale's default pool
  name and stays.
* Inodes, sizes, times and modes are kept: they carry no names, and they are
  what the tests need.

Before writing anything it checks its own output: every name it replaced is
searched for, and if one survived, nothing is written and it exits 1. That is
the leak test that matters, because it runs where the raw input is. The
output starts with the header scripts/check_raw_data.py requires, is written
mode 0600, and may not be a .raw file or the input itself.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cdm import policy  # noqa: E402

VERSION = "1"
KEEP_EXTENSIONS = frozenset(
    "txt csv tsv json yaml yml xml md log out err py c h cc cpp hpp f f90 sh o a so "
    "gz bz2 xz zst tar zip tgz dmg iso img gguf safetensors bin pt pth onnx npz npy "
    "h5 hdf5 nc dat parquet jpg jpeg png tif tiff pdf mp4 mov wav".split())
KEEP_COMPONENTS = frozenset({"gpfs"})
# Tokens shorter than this are not searched for in the leak check: a two-letter
# directory name would match all over any file.
MIN_TOKEN = 3
# Words the sanitizer itself writes. A replaced name that happens to be one of
# these (a directory called `system`) was still replaced, so finding the word
# in the output is not a leak.
OWN_WORDS = frozenset(
    "gpfs fs1 cluster1 example system data fileset sanitized sanitize cdm policy "
    "listing device cluster scope suppress_atime fields times utc generated "
    "generator end rows relatime yes no".split())


class Sanitizer:
    def __init__(self, header: dict[str, str]):
        self.names: dict[tuple[str, str], str] = {}
        self.counts = {"d": 0, "f": 0, "l": 0}
        self.ids: dict[int, int] = {0: 0}
        self.filesets: dict[str, str] = {}
        self.pools: dict[str, str] = {"system": "system"}
        self.numbered = {"fileset": 0, "data": 0}
        self.device = header["device"]
        self.cluster = header["cluster"]

    def _id(self, n: int) -> int:
        if n not in self.ids:
            self.ids[n] = 1000 + len(self.ids) - 1
        return self.ids[n]

    def _mapped(self, table: dict[str, str], name: str, prefix: str) -> str:
        if name not in table:
            self.numbered[prefix] += 1
            table[name] = f"{prefix}{self.numbered[prefix]}"
        return table[name]

    def fileset(self, name: str) -> str:
        return self._mapped(self.filesets, name, "fileset")

    def pool(self, name: str) -> str:
        return self._mapped(self.pools, name, "data")

    def component(self, name: str, kind: str) -> str:
        if name in KEEP_COMPONENTS:
            return name
        if name == self.device:
            return "fs1"
        if name in self.filesets:
            return self.filesets[name]
        key = (kind, name)
        if key not in self.names:
            self.counts[kind] += 1
            hidden = "." if name.startswith(".") else ""
            stem, dot, ext = name.lstrip(".").rpartition(".")
            keep = f".{ext.lower()}" if dot and stem and ext.lower() in KEEP_EXTENSIONS else ""
            self.names[key] = f"{hidden}{kind}{self.counts[kind]:04d}{keep}"
        return self.names[key]

    def path(self, path: str, kind: str) -> str:
        parts = path.strip("/").split("/")
        out = [self.component(p, "d") for p in parts[:-1]]
        out.append(self.component(parts[-1], {"dir": "d", "link": "l"}.get(kind, "f")))
        return "/" + "/".join(out)


_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/")


def _escape(text: str) -> str:
    """Percent-encode as mmapplypolicy's ESCAPE '%/' does (measured): bytes
    outside the URL-unreserved set, plus the `/` it is told to keep, become %XX."""
    return "".join(chr(b) if b in _SAFE else f"%{b:02X}" for b in os.fsencode(text))


def _utc(x: float) -> str:
    return datetime.fromtimestamp(x, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _fields(e: policy.Entry, s: Sanitizer) -> str:
    t = _utc
    values = [str(e.size), t(e.mtime), t(e.atime), t(e.ctime), str(s._id(e.uid)),
              str(s._id(e.gid)), e.mode, s.fileset(e.fileset), s.pool(e.pool), str(e.nlink)]
    return _escape("|".join(values))


def sanitize(lines) -> tuple[list[str], set[str], int]:
    """(output lines, every name in the input, entries). Raises ListingError.

    The names are collected from the input itself, not from the replacing code:
    a bug that forgot to replace a name would also forget to report it, and the
    leak check would pass it. Measured: it did, until this was separated.
    """
    header, entries, state = policy.read_listing(lines)
    s = Sanitizer(header)
    names = {header["device"], header["cluster"], *header["cluster"].split("."),
             *header["scope"].split()[1:]}
    body = []
    for e in entries:
        names.update(p for p in e.path.split("/") if p)
        names.update(p.lstrip(".").rpartition(".")[0] for p in e.path.split("/")
                     if "." in p.lstrip("."))
        names.update({e.fileset, e.pool})
        # Fields first: they register the fileset name that paths may contain.
        fields = _fields(e, s)
        path = _escape(s.path(e.path, e.kind))
        body.append(f"{e.inode} {e.gen} {e.snapid}  {fields} -- {path}\n")
    if not state["complete"]:
        raise policy.ListingError("the listing has no end marker: sanitize only a "
                                  "complete listing")
    scope = header["scope"]
    if scope.startswith("fileset "):
        scope = f"fileset {s.fileset(scope[len('fileset '):])}"
    head = [f"{policy.SANITIZED}{VERSION}\n", f"# {policy.FORMAT}\n",
            "# device: fs1\n", "# cluster: cluster1.example\n", f"# scope: {scope}\n",
            f"# suppress_atime: {header['suppress_atime']}\n",
            f"# fields: {header['fields']}\n", "# times: utc\n",
            f"# generated: {header.get('generated', '')}\n",
            "# generator: cdm-sanitize\n"]
    names.discard("")
    return head + body + [f"{policy.END}{len(body)}\n"], names, len(body)


def _searchable(text: str) -> str:
    """The parts of a sanitized listing that could carry a name.

    Header lines, and each entry's path (decoded and as written), mode, fileset
    and pool. Not the numbers -- inode, size, times, ids, links -- which are
    kept on purpose and cannot hold a name, but would otherwise make a real
    directory called `2026` look like a leak inside a timestamp.
    """
    parts = []
    for line in text.splitlines():
        if line.startswith("#"):
            # Not the two numeric header lines: when it was made, how many rows.
            if not line.startswith(("# generated: ", policy.END)):
                parts.append(line)
            continue
        e = policy.parse_entry(line)
        parts += [e.path, line.partition(" -- ")[2], e.mode, e.fileset, e.pool]
    return "\n".join(parts)


def leaks(text: str, secrets: set[str]) -> list[str]:
    """Input names that still appear in a sanitized listing, as whole words,
    ignoring case, plain or percent-encoded."""
    found = []
    lowered = _searchable(text).lower()
    for token in secrets:
        if len(token) < MIN_TOKEN or token.lower() in OWN_WORDS:
            continue
        pattern = r"(?<![A-Za-z0-9])" + re.escape(token.lower()) + r"(?![A-Za-z0-9])"
        # Search both the plain and the percent-encoded spelling.
        if re.search(pattern, lowered) or _escape(token).lower() in lowered \
                and _escape(token) != token:
            found.append(token)
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: sanitize_listing.py RAW.list.raw OUT.list", file=sys.stderr)
        return 2
    src, dst = Path(argv[0]), Path(argv[1])
    if dst.name.endswith(".raw"):
        print("sanitize_listing: the output must not be a .raw file", file=sys.stderr)
        return 2
    if dst.exists() and dst.resolve() == src.resolve():
        print("sanitize_listing: refusing to overwrite the input", file=sys.stderr)
        return 2
    try:
        with src.open(encoding="utf-8", errors="surrogateescape") as f:
            out, secrets, entries = sanitize(f)
    except (OSError, policy.ListingError) as exc:
        print(f"sanitize_listing: {exc}", file=sys.stderr)
        return 1
    text = "".join(out)
    survived = leaks(text, secrets)
    if survived:
        # Deliberately not printing them: this output may be pasted anywhere.
        print(f"sanitize_listing: {len(survived)} replaced name(s) still appear in "
              f"the output; nothing was written", file=sys.stderr)
        return 1
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)
    print(f"sanitize_listing: wrote {dst} ({entries} entries, "
          f"{len(secrets)} names replaced, leak check passed)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
