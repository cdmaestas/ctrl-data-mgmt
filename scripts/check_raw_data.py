#!/usr/bin/env python3
"""Refuse raw cluster data: the guard behind the pre-commit hook and CI.

A policy listing from a real cluster names every file on it, and export dumps
name every file on a host. Only sanitized fixtures may be committed (see
tests/fixtures/README.md). `.gitignore` keeps raw files out of `git add`, but
`git add -f` and `git commit --no-verify` get past that, so this runs twice:
on staged files in the pre-commit hook, and on every tracked file in CI.

    check_raw_data.py --staged    what is about to be committed
    check_raw_data.py --all       everything tracked (CI)

Exit 0 when clean, 1 with one line per problem otherwise, 2 on bad usage or a
git failure. Stdlib only, so it runs before anything is installed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import PurePosixPath

# Every sanitized fixture starts with this; the sanitizer writes it. The
# version lets a later sanitizer change what it scrubs without old fixtures
# passing as new ones.
HEADER = "# sanitized by cdm-sanitize v"
FIXTURES = PurePosixPath("tests/fixtures")
RAW_DIR = FIXTURES / "raw"
EXEMPT = {FIXTURES / "README.md"}

# One line of mmapplypolicy LIST output: inode, generation and snapshot id,
# then the SHOW() fields, then " -- " and the path.
_LIST_LINE = re.compile(rb"^\s*\d+\s+\d+\s+-?\d+\s.*\s--\s+\S", re.M)
LIST_LINES_TO_FLAG = 3


def problems(path: str, content: bytes) -> list[str]:
    """Why `path` with `content` must not be committed; empty if it may be."""
    p = PurePosixPath(path)
    found = []
    if p.name.endswith(".raw"):
        found.append(f"{path}: raw file (*.raw) -- sanitize it, never commit the raw one")
    if RAW_DIR in p.parents:
        found.append(f"{path}: under {RAW_DIR}/, which holds unsanitized input only")
    if ".cdm-export" in p.name:
        found.append(f"{path}: a cdm export dump names every file on a host")
    in_fixtures = FIXTURES in p.parents and p not in EXEMPT
    sanitized = content.startswith(HEADER.encode())
    if in_fixtures and not sanitized and RAW_DIR not in p.parents:
        found.append(f"{path}: fixture without the '{HEADER}N' header -- "
                     f"only sanitizer output belongs in {FIXTURES}/")
    # A raw listing under an innocent name, anywhere in the tree.
    if not sanitized and b"\0" not in content[:8192]:
        n = len(_LIST_LINE.findall(content))
        if n >= LIST_LINES_TO_FLAG:
            found.append(f"{path}: {n} lines look like raw mmapplypolicy LIST output")
    return found


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def staged() -> list[tuple[str, bytes]]:
    """Files being added or changed by the next commit, as staged (not as on disk)."""
    names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [(n, _git("show", f":{n}")) for n in names.decode().split("\0") if n]


def tracked() -> list[tuple[str, bytes]]:
    names = _git("ls-files", "-z").decode().split("\0")
    out = []
    for n in filter(None, names):
        try:
            with open(n, "rb") as f:
                out.append((n, f.read()))
        except FileNotFoundError:
            continue   # deleted in the working tree; nothing to leak
    return out


def main(argv: list[str]) -> int:
    if argv not in (["--staged"], ["--all"]):
        print("usage: check_raw_data.py --staged | --all", file=sys.stderr)
        return 2
    try:
        files = staged() if argv == ["--staged"] else tracked()
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"check_raw_data: git failed: {exc}", file=sys.stderr)
        return 2
    found = [msg for name, content in files for msg in problems(name, content)]
    for msg in found:
        print(msg, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
