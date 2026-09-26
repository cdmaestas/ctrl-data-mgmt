"""Two hashes, for two different questions.

PARTIAL (default) answers "are these probably the same file". It reads the
first and last 64 KB (all of it, up to 128 KB) and mixes the size in. On a
multi-terabyte tree that is the difference between minutes and a weekend, and
for dedupe it is very nearly as good as a full hash: two distinct files that
share both ends *and* their exact byte count are rare enough that
`cdm dupes --verify` exists to settle it.

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
    """Hash of (size, first 64 KB, last 64 KB). Cheap and stable.

    A file of at most two windows is hashed whole: head and tail would overlap
    or touch, and reading just the head would leave bytes WINDOW..size unhashed.
    For those files the digest is the full content under the partial tag, which
    is also exactly what v1 produced for size <= WINDOW; see db._migrate for the
    rows v1 got wrong.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(b"cdm-partial-v1\0")
    h.update(str(size).encode("ascii"))
    with open(path, "rb") as f:
        if size <= 2 * WINDOW:
            h.update(f.read(2 * WINDOW))
        else:
            h.update(f.read(WINDOW))
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


def bytes_read(size: int, kind: str) -> int:
    """How many bytes compute() reads for a file of `size`, for throughput."""
    if kind == FULL:
        return size
    return size if size <= 2 * WINDOW else 2 * WINDOW


def compute(path: Path, size: int, kind: str) -> str:
    if kind == FULL:
        return full_hash(path)
    if kind == PARTIAL:
        return partial_hash(path, size)
    raise ValueError(f"unknown hash kind: {kind!r}")
