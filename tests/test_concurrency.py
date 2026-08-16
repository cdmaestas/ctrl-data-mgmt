"""Threaded walking, checkpoint/resume, and the latency probe.

These are the parts where a bug is silent: a threaded walk that drops entries
under contention, or a resume that skips a directory whose rows were never
committed, both produce an index that looks fine and is wrong.
"""
from __future__ import annotations

import threading

import pytest

from cdm import db, hashing, probe
from cdm.scan import scan_root

HOST = "testhost"


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "index.db")
    yield c
    c.close()


def wide_tree(base, dirs=12, per_dir=40):
    """A tree wide enough that threads genuinely interleave."""
    base.mkdir(parents=True, exist_ok=True)
    for d in range(dirs):
        sub = base / f"d{d:02d}"
        sub.mkdir()
        for f in range(per_dir):
            (sub / f"f{f:02d}.txt").write_text(f"{d}-{f}")
    return dirs, dirs * per_dir


# --- threading ------------------------------------------------------------

@pytest.mark.parametrize("threads", [1, 2, 4, 8])
def test_thread_count_does_not_change_the_result(conn, tmp_path, threads):
    root = tmp_path / f"tree{threads}"
    dirs, files = wide_tree(root)

    stats = scan_root(conn, HOST, root, threads=threads)
    assert (stats.dirs, stats.files) == (dirs, files)

    indexed = conn.execute(
        "SELECT COUNT(*) FROM files WHERE root = ?", (str(root),)).fetchone()[0]
    assert indexed == dirs + files


def test_threaded_scan_records_every_path_exactly_once(conn, tmp_path):
    root = tmp_path / "tree"
    wide_tree(root, dirs=10, per_dir=30)
    scan_root(conn, HOST, root, threads=8)

    rows = conn.execute("SELECT path FROM files WHERE root = ?",
                        (str(root),)).fetchall()
    paths = [r[0] for r in rows]
    assert len(paths) == len(set(paths)), "a path was indexed twice"

    on_disk = {str(p) for p in root.rglob("*")}
    assert set(paths) == on_disk


def test_threaded_hashing_is_correct(conn, tmp_path):
    root = tmp_path / "tree"
    wide_tree(root, dirs=6, per_dir=20)
    scan_root(conn, HOST, root, threads=8, hash_kind=hashing.PARTIAL)

    unhashed = conn.execute(
        "SELECT COUNT(*) FROM files WHERE type = 'file' AND hash IS NULL"
    ).fetchone()[0]
    assert unhashed == 0

    row = conn.execute(
        "SELECT path, hash, size FROM files WHERE type = 'file' LIMIT 1").fetchone()
    from pathlib import Path
    assert row["hash"] == hashing.partial_hash(Path(row["path"]), row["size"])


def test_threaded_scan_leaves_no_threads_behind(conn, tmp_path):
    root = tmp_path / "tree"
    wide_tree(root, dirs=6, per_dir=10)
    before = threading.active_count()
    scan_root(conn, HOST, root, threads=8)
    assert threading.active_count() == before


def test_exclusions_hold_under_threads(conn, tmp_path):
    root = tmp_path / "home"
    wide_tree(root, dirs=4, per_dir=10)
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_rsa").write_text("PRIVATE")

    scan_root(conn, HOST, root, threads=8)
    names = {r[0] for r in conn.execute("SELECT name FROM files")}
    assert "id_rsa" not in names
    assert ".ssh" not in names


# --- checkpoint and resume -------------------------------------------------

def test_completed_scan_leaves_no_checkpoint(conn, tmp_path):
    root = tmp_path / "tree"
    wide_tree(root, dirs=4, per_dir=5)
    scan_root(conn, HOST, root)

    assert conn.execute("SELECT COUNT(*) FROM scan_dirs").fetchone()[0] == 0
    unfinished = conn.execute(
        "SELECT COUNT(*) FROM scans WHERE finished_at IS NULL").fetchone()[0]
    assert unfinished == 0


def test_resume_skips_directories_already_recorded(conn, tmp_path):
    root = tmp_path / "tree"
    wide_tree(root, dirs=6, per_dir=5)

    # Simulate an interrupted scan: an open scan row plus checkpoints for the
    # directories it had finished, with their rows committed.
    scan_root(conn, HOST, root)
    stamp = conn.execute("SELECT last_scan FROM roots").fetchone()[0]
    conn.execute(
        "INSERT INTO scans (host, root, scan_id, started_at, hash_kind) "
        "VALUES (?,?,?,?,?)", (HOST, str(root), "abc123", stamp, None))
    done = [str(root), str(root / "d00"), str(root / "d01")]
    conn.executemany(
        "INSERT INTO scan_dirs (host, root, scan_id, path) VALUES (?,?,?,?)",
        [(HOST, str(root), "abc123", p) for p in done])
    conn.commit()

    stats = scan_root(conn, HOST, root, resume=True, now=stamp)
    assert stats.resumed_from == 3
    # The skipped directories' children are still reached through the index.
    indexed = conn.execute(
        "SELECT COUNT(*) FROM files WHERE root = ?", (str(root),)).fetchone()[0]
    assert indexed == 6 + 6 * 5


def test_restart_ignores_the_checkpoint(conn, tmp_path):
    root = tmp_path / "tree"
    wide_tree(root, dirs=3, per_dir=4)
    scan_root(conn, HOST, root)
    stamp = conn.execute("SELECT last_scan FROM roots").fetchone()[0]
    conn.execute(
        "INSERT INTO scans (host, root, scan_id, started_at, hash_kind) "
        "VALUES (?,?,?,?,?)", (HOST, str(root), "zzz", stamp, None))
    conn.execute(
        "INSERT INTO scan_dirs (host, root, scan_id, path) VALUES (?,?,?,?)",
        (HOST, str(root), "zzz", str(root / "d00")))
    conn.commit()

    stats = scan_root(conn, HOST, root, resume=False)
    assert stats.resumed_from == 0
    assert stats.dirs == 3


def test_resume_refuses_a_different_hash_kind(conn, tmp_path):
    """Resuming a stat-only scan with --checksum would leave half a tree hashed."""
    root = tmp_path / "tree"
    wide_tree(root, dirs=3, per_dir=4)
    conn.execute(
        "INSERT INTO scans (host, root, scan_id, started_at, hash_kind) "
        "VALUES (?,?,?,?,?)", (HOST, str(root), "open1", "2020-01-01T00:00:00", None))
    conn.execute(
        "INSERT INTO scan_dirs (host, root, scan_id, path) VALUES (?,?,?,?)",
        (HOST, str(root), "open1", str(root / "d00")))
    conn.commit()

    stats = scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL, resume=True)
    assert stats.resumed_from == 0, "resumed a scan with a different hash kind"


def test_checkpoint_rows_are_scoped_to_one_scan(conn, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    wide_tree(a, dirs=2, per_dir=3)
    wide_tree(b, dirs=2, per_dir=3)
    scan_root(conn, HOST, a)
    scan_root(conn, HOST, b)
    assert conn.execute("SELECT COUNT(*) FROM scan_dirs").fetchone()[0] == 0


# --- probe -----------------------------------------------------------------

def test_probe_on_a_local_tree_recommends_one_thread(tmp_path):
    root = tmp_path / "tree"
    wide_tree(root, dirs=4, per_dir=30)
    result = probe.probe(root)
    assert result.sampled > 0
    assert result.is_local
    assert result.threads == 1


def test_probe_describes_itself(tmp_path):
    root = tmp_path / "tree"
    wide_tree(root, dirs=2, per_dir=5)
    text = probe.probe(root).describe()
    assert "ms/stat" in text and "thread" in text


def test_probe_of_an_empty_directory_is_safe(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = probe.probe(empty)
    assert result.sampled == 0
    assert result.threads == 1


def test_slow_filesystem_gets_more_threads(monkeypatch, tmp_path):
    """The classifier, not the clock: a slow stat must raise the thread count."""
    root = tmp_path / "tree"
    wide_tree(root, dirs=2, per_dir=20)

    real = probe.time.perf_counter
    step = [0.0]

    def creeping():
        step[0] += 0.0005   # 0.5ms per call pair
        return real() + step[0]

    monkeypatch.setattr(probe.time, "perf_counter", creeping)
    result = probe.probe(root, sample=20)
    assert not result.is_local
    assert result.threads > 1
