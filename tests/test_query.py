"""Parsing, filtering, and the partial-hash/full-hash boundary."""
from __future__ import annotations

import time

import pytest

from cdm import db, hashing, query
from cdm.scan import scan_root

HOST = "testhost"


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "index.db")
    yield c
    c.close()


# --- parsing ---------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("4096", 4096),
    ("1k", 1024),
    ("100M", 100 * 1024 ** 2),
    ("2.5G", int(2.5 * 1024 ** 3)),
    ("1TB", 1024 ** 4),
])
def test_parse_size(text, expected):
    assert query.parse_size(text) == expected


@pytest.mark.parametrize("bad", ["", "big", "100X", "M100"])
def test_parse_size_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        query.parse_size(bad)


def test_parse_when_relative():
    now = 1_000_000.0
    assert query.parse_when("7d", now=now) == now - 7 * 86400
    assert query.parse_when("24h", now=now) == now - 86400


def test_parse_when_absolute():
    assert query.parse_when("2026-08-01") == time.mktime(
        time.strptime("2026-08-01", "%Y-%m-%d"))


def test_parse_when_rejects_nonsense():
    with pytest.raises(ValueError):
        query.parse_when("last tuesday")


# --- find ------------------------------------------------------------------

@pytest.fixture()
def indexed(conn, tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "small.txt").write_text("a")
    (root / "large.bin").write_bytes(b"x" * 8192)
    (root / "notes.csv").write_text("b" * 100)
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    return conn, root


def test_find_by_size(indexed):
    conn, _ = indexed
    rows = query.find(conn, larger_than=1000)
    assert {r["name"] for r in rows} == {"large.bin"}


def test_find_by_name_glob(indexed):
    conn, _ = indexed
    rows = query.find(conn, name="*.csv")
    assert {r["name"] for r in rows} == {"notes.csv"}


def test_find_by_type_excludes_dirs(indexed):
    conn, _ = indexed
    rows = query.find(conn, kind="file")
    assert all(r["type"] == "file" for r in rows)


def test_find_orders_by_size_descending(indexed):
    conn, _ = indexed
    rows = query.find(conn, kind="file", order="size")
    sizes = [r["size"] for r in rows]
    assert sizes == sorted(sizes, reverse=True)


def test_find_limit(indexed):
    conn, _ = indexed
    assert len(query.find(conn, kind="file", limit=1)) == 1


# --- dupes -----------------------------------------------------------------

def test_dupes_finds_identical_files(conn, tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "one.txt").write_text("same content here")
    (root / "two.txt").write_text("same content here")
    (root / "other.txt").write_text("different")
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)

    groups = query.dupe_groups(conn)
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert {r["name"] for r in groups[0]["members"]} == {"one.txt", "two.txt"}


def test_dupes_ignores_empty_files_by_default(conn, tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "e1").write_text("")
    (root / "e2").write_text("")
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    assert query.dupe_groups(conn) == []


def test_partial_and_full_hashes_never_pool(conn, tmp_path):
    """A partial digest and a full digest must not be grouped together."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "one.txt").write_text("identical")
    (root / "two.txt").write_text("identical")
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    # Rewrite one row as though it had been hashed in full.
    conn.execute("UPDATE files SET hash_kind = ? WHERE name = 'two.txt'",
                 (hashing.FULL,))
    conn.commit()
    assert query.dupe_groups(conn) == []


def test_verify_splits_false_partial_matches(conn, tmp_path):
    """Same size, same ends, different middle: partial says maybe, full says no."""
    root = tmp_path / "tree"
    root.mkdir()
    head, tail = b"H" * 70000, b"T" * 70000
    (root / "a.bin").write_bytes(head + b"AAAA" + tail)
    (root / "b.bin").write_bytes(head + b"BBBB" + tail)
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)

    groups = query.dupe_groups(conn)
    assert len(groups) == 1 and groups[0]["count"] == 2   # partial hash matched
    assert query.verify_group(groups[0]) == []            # full hash says no


def test_verify_confirms_true_duplicates(conn, tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"z" * 200000)
    (root / "b.bin").write_bytes(b"z" * 200000)
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)

    confirmed = query.verify_group(query.dupe_groups(conn)[0])
    assert len(confirmed) == 1 and len(confirmed[0]) == 2


# --- staleness -------------------------------------------------------------

def test_hash_goes_stale_when_the_file_changes(conn, tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("before")
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)

    row = query.stat_one(conn, str(target))
    assert not query.hash_is_stale(row)

    conn.execute("UPDATE files SET size = size + 1 WHERE name = 'a.txt'")
    conn.commit()
    assert query.hash_is_stale(query.stat_one(conn, str(target)))


def test_unhashed_rows_are_not_stale(conn, tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("x")
    scan_root(conn, HOST, root)
    assert not query.hash_is_stale(query.stat_one(conn, str(root / "a.txt")))
