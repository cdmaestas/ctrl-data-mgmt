"""Two hashes, for two different questions.

PARTIAL (default) answers "are these probably the same file". It reads the
first and last 64 KB and mixes the size in. On a multi-terabyte tree that is
the difference between minutes and a weekend, and for dedupe it is very nearly
as good as a full hash: two distinct files that share both ends *and* their
exact byte count are rare enough that `cdm dupes --verify` exists to settle it.

FULL answers "is this byte-for-byte what I recorded". No shortcut is available
and none is offered.

Which one produced a given row is recorded in files.hash_kind, so the two can
never be confused. Digests are domain-separated -- a partial digest and a full
digest of the same file are deliberately different values.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

WINDOW = 64 * 1024  # bytes read from each end for a partial hash
CHUNK = 1024 * 1024

PARTIAL = "partial"
FULL = "full"


def partial_hash(path: Path, size: int) -> str:
    """Hash of (size, first 64 KB, last 64 KB). Cheap and stable."""
    h = hashlib.blake2b(digest_size=16)
    h.update(b"cdm-partial-v1\0")
    h.update(str(size).encode("ascii"))
    with open(path, "rb") as f:
        head = f.read(WINDOW)
        h.update(head)
        # Only seek for the tail when the file is big enough for the two
        # windows to be disjoint; otherwise head already covers the whole file.
        if size > 2 * WINDOW:
            f.seek(-WINDOW, 2)
            h.update(f.read(WINDOW))
    return h.hexdigest()


def full_hash(path: Path) -> str:
    """Hash of every byte."""
    h = hashlib.blake2b(digest_size=16)
    h.update(b"cdm-full-v1\0")
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def compute(path: Path, size: int, kind: str) -> str:
    if kind == FULL:
        return full_hash(path)
    if kind == PARTIAL:
        return partial_hash(path, size)
    raise ValueError(f"unknown hash kind: {kind!r}")
