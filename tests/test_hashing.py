"""Partial hashing: which bytes it covers, and what it reports reading."""
from __future__ import annotations

import builtins

import pytest

from cdm import db, hashing

W = hashing.WINDOW


def test_partial_hash_covers_bytes_past_the_first_window(tmp_path):
    # 100 KiB is between one and two windows: head alone would miss byte 80,000.
    data = bytearray(b"\0" * (100 * 1024))
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(data)
    data[80_000] = 1
    b.write_bytes(data)
    size = len(data)
    assert hashing.partial_hash(a, size) != hashing.partial_hash(b, size)


@pytest.mark.parametrize("size", [1, W - 1, W, W + 1, 2 * W - 1, 2 * W])
def test_files_up_to_two_windows_are_hashed_whole(tmp_path, size):
    p = tmp_path / "f"
    p.write_bytes(bytes(size))
    before = hashing.partial_hash(p, size)
    for i in {0, size // 2, W, size - 1} & set(range(size)):
        changed = bytearray(size)
        changed[i] = 1
        p.write_bytes(changed)
        assert hashing.partial_hash(p, size) != before, (size, i)


@pytest.mark.parametrize("size", [0, 10, W, W + 1, 100 * 1024, 2 * W, 2 * W + 1, 5 * W])
def test_bytes_read_matches_what_partial_hash_reads(tmp_path, monkeypatch, size):
    p = tmp_path / "f"
    p.write_bytes(bytes(size))
    total = [0]
    real_open = builtins.open

    class Counting:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def read(self, n=-1):
            block = self.f.read(n)
            total[0] += len(block)
            return block

        def seek(self, *a):
            return self.f.seek(*a)

    # hashing opens through atime.open_quietly, so that is what gets counted.
    monkeypatch.setattr(hashing, "open_quietly",
                        lambda path: Counting(real_open(path, "rb")))
    hashing.partial_hash(p, size)
    assert total[0] == hashing.bytes_read(size, hashing.PARTIAL)


def test_migration_drops_only_partial_hashes_v2_got_wrong(tmp_path):
    path = tmp_path / "index.db"
    conn = db.connect(path)
    rows = [  # (name, hash_size, hash_kind): only "mid" was hashed wrongly
        ("small", W, hashing.PARTIAL),
        ("mid", W + 1, hashing.PARTIAL),
        ("edge", 2 * W, hashing.PARTIAL),
        ("big", 2 * W + 1, hashing.PARTIAL),
        ("full", W + 1, hashing.FULL),
    ]
    for name, size, kind in rows:
        conn.execute(
            "INSERT INTO files (host, root, path, parent, name, size, mtime, ctime, "
            "type, hash, hash_kind, hash_size, hash_mtime, seen_at) "
            "VALUES ('h', '/r', ?, '/r', ?, ?, 1, 1, 'file', 'd', ?, ?, 1, 'now')",
            ("/r/" + name, name, size, kind, size))
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    kept = {r["name"]: r["hash"] for r in conn.execute("SELECT name, hash FROM files")}
    assert kept == {"small": "d", "mid": None, "edge": None, "big": "d", "full": "d"}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()
