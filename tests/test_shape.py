"""The shape functions: correct numbers, and totals that always add up."""
from __future__ import annotations

import os
import time

import pytest

from cdm import db, hashing, shape
from cdm.scan import scan_root

HOST = "testhost"
DAY = 86400


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "index.db")
    yield c
    c.close()


def write(path, nbytes, *, age_days=0.0, now=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * nbytes)
    if age_days:
        t = (now or time.time()) - age_days * DAY
        os.utime(path, (t, t))
    return path


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "data"
    write(root / "tiny.txt", 10)
    write(root / "a" / "mid.csv", 5000)
    write(root / "a" / "mid2.csv", 6000)
    write(root / "b" / "big.bin", 2 * 1024 * 1024)
    return root


# --- summary ---------------------------------------------------------------

def test_summary_counts_per_root(conn, tree):
    scan_root(conn, HOST, tree)
    out = shape.summary(conn, host=HOST)
    [r] = out["roots"]
    assert (r["files"], r["dirs"]) == (4, 2)
    assert r["bytes"] == 10 + 5000 + 6000 + 2 * 1024 * 1024
    assert out["total_files"] == 4


def test_summary_excludes_directory_inode_sizes(conn, tree):
    scan_root(conn, HOST, tree)
    [r] = shape.summary(conn, host=HOST)["roots"]
    assert r["bytes"] == sum(p.stat().st_size for p in tree.rglob("*") if p.is_file())


def test_summary_reports_scan_staleness(conn, tree):
    scan_root(conn, HOST, tree)
    later = time.time() + 3 * DAY
    [r] = shape.summary(conn, host=HOST, now=later)["roots"]
    assert 2.9 < r["last_scan_age_days"] < 3.1


def test_summary_scoped_to_one_root(conn, tree, tmp_path):
    other = tmp_path / "other"
    write(other / "x.dat", 100)
    scan_root(conn, HOST, tree)
    scan_root(conn, HOST, other)
    out = shape.summary(conn, host=HOST, root=str(other.resolve()))
    assert [r["root"] for r in out["roots"]] == [str(other.resolve())]
    assert out["total_bytes"] == 100


# --- histograms ------------------------------------------------------------

def test_size_histogram_buckets(conn, tree):
    scan_root(conn, HOST, tree)
    out = shape.size_histogram(conn, host=HOST)
    got = {b["bucket"]: b["files"] for b in out["buckets"]}
    assert got["<4K"] == 1
    assert got["4K-1M"] == 2
    assert got["1M-100M"] == 1
    assert got[">=10G"] == 0


def test_size_histogram_lists_every_bucket_even_empty(conn, tree):
    scan_root(conn, HOST, tree)
    labels = [b["bucket"] for b in shape.size_histogram(conn, host=HOST)["buckets"]]
    assert labels == [label for label, _ in shape.SIZE_BUCKETS]


def test_size_histogram_totals_add_up(conn, tree):
    scan_root(conn, HOST, tree)
    out = shape.size_histogram(conn, host=HOST)
    s = shape.summary(conn, host=HOST)
    assert out["total_files"] == s["total_files"]
    assert out["total_bytes"] == s["total_bytes"]


def test_age_histogram_buckets(conn, tmp_path):
    now = time.time()
    root = tmp_path / "aged"
    write(root / "new", 1, age_days=1, now=now)
    write(root / "month", 1, age_days=45, now=now)
    write(root / "old", 1, age_days=800, now=now)
    write(root / "ancient", 1, age_days=2000, now=now)
    scan_root(conn, HOST, root)

    got = {b["bucket"]: b["files"]
           for b in shape.age_histogram(conn, host=HOST, now=now)["buckets"]}
    assert got["<7d"] == 1
    assert got["30d-90d"] == 1
    assert got["1y-3y"] == 1
    assert got[">=3y"] == 1


def test_future_mtimes_get_their_own_bucket(conn, tmp_path):
    """Clock skew is reported, never folded into 'recent'."""
    now = time.time()
    root = tmp_path / "skew"
    write(root / "from-the-future", 1, age_days=-10, now=now)
    scan_root(conn, HOST, root)
    got = {b["bucket"]: b["files"]
           for b in shape.age_histogram(conn, host=HOST, now=now)["buckets"]}
    assert got[shape.FUTURE] == 1
    assert got["<7d"] == 0


# --- extensions ------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("report.pdf", "pdf"),
    ("PHOTO.JPG", "jpg"),              # case folded
    ("archive.tar.gz", "gz"),          # last suffix only
    ("Makefile", shape.NO_EXTENSION),
    (".bashrc", shape.NO_EXTENSION),   # a dotfile, not an extension
    ("notes.JohnSmith", shape.OTHER_EXTENSION),    # 9 chars: a name, not a type
    ("v1.2-final-draft", shape.OTHER_EXTENSION),   # punctuation: not a type
    ("data.", shape.OTHER_EXTENSION),
])
def test_extension_of(name, expected):
    assert shape.extension_of(name) == expected


def test_extensions_ranked_by_bytes(conn, tree):
    scan_root(conn, HOST, tree)
    out = shape.extensions(conn, host=HOST)
    # big.bin is a singleton extension, so it is folded into (other).
    first = out["extensions"][0]
    assert first["extension"] == shape.OTHER_EXTENSION
    assert {e["extension"] for e in out["extensions"]} >= {"csv"}


def test_singleton_extensions_fold_into_other(conn, tmp_path):
    root = tmp_path / "t"
    write(root / "a.csv", 1)
    write(root / "b.csv", 1)
    write(root / "c.xyz", 1)          # only one .xyz: more likely a name than a type
    scan_root(conn, HOST, root)
    exts = {e["extension"] for e in shape.extensions(conn, host=HOST)["extensions"]}
    assert "csv" in exts
    assert "xyz" not in exts
    assert shape.OTHER_EXTENSION in exts


def test_extensions_totals_add_up_even_when_truncated(conn, tmp_path):
    root = tmp_path / "t"
    for ext in ("aa", "bb", "cc", "dd"):
        write(root / f"1.{ext}", 10)
        write(root / f"2.{ext}", 10)
    scan_root(conn, HOST, root)
    out = shape.extensions(conn, host=HOST, limit=2)
    shown = sum(e["files"] for e in out["extensions"])
    assert len(out["extensions"]) == 2
    assert out["remainder"]["extensions"] == 2
    assert shown + out["remainder"]["files"] == out["total_files"] == 8


# --- duplicates -------------------------------------------------------------

def test_duplicates_summary_separates_candidates_from_confirmed(conn, tmp_path):
    root = tmp_path / "d"
    write(root / "one", 4096)
    write(root / "two", 4096)
    write(root / "unique", 999)
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    out = shape.duplicates_summary(conn, host=HOST)
    assert out["candidates"]["groups"] == 1
    assert out["candidates"]["reclaimable_bytes"] == 4096
    assert out["confirmed"]["groups"] == 0


def test_duplicates_summary_reports_hash_coverage(conn, tree):
    """'No duplicates' from an unhashed index means nothing; say so."""
    scan_root(conn, HOST, tree)
    cov = shape.duplicates_summary(conn, host=HOST)["coverage"]
    assert cov["files"] == 4
    assert cov["partial_hashed"] == cov["full_hashed"] == 0
