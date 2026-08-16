"""Scanner behaviour: what it records, what it refuses, what it reuses."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from cdm import db, hashing
from cdm.exclude import Excluder
from cdm.scan import scan_root

HOST = "testhost"


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "index.db")
    yield c
    c.close()


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("alpha")
    (root / "sub" / "b.txt").write_text("beta")
    (root / "sub" / "big.bin").write_bytes(b"x" * 4096)
    return root


def paths_in(conn, kind=None):
    sql = "SELECT path FROM files"
    args = ()
    if kind:
        sql += " WHERE type = ?"
        args = (kind,)
    return {Path(r["path"]).name for r in conn.execute(sql, args)}


def test_records_files_and_dirs(conn, tree):
    stats = scan_root(conn, HOST, tree)
    assert stats.files == 3
    assert stats.dirs == 1
    assert paths_in(conn, "file") == {"a.txt", "b.txt", "big.bin"}
    assert paths_in(conn, "dir") == {"sub"}


def test_root_itself_is_not_a_row(conn, tree):
    """The root is in `roots`, not in `files` -- it is not a thing you found."""
    scan_root(conn, HOST, tree)
    assert conn.execute("SELECT COUNT(*) FROM roots").fetchone()[0] == 1
    assert Path(tree).name not in paths_in(conn)


def test_no_hashes_by_default(conn, tree):
    scan_root(conn, HOST, tree)
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE hash IS NOT NULL").fetchone()[0] == 0


def test_partial_hash_records_what_it_hashed(conn, tree):
    scan_root(conn, HOST, tree, hash_kind=hashing.PARTIAL)
    row = conn.execute("SELECT * FROM files WHERE name = 'a.txt'").fetchone()
    assert row["hash_kind"] == hashing.PARTIAL
    assert row["hash_size"] == row["size"]
    assert row["hash_mtime"] == row["mtime"]


def test_rescan_reuses_unchanged_hashes(conn, tree):
    first = scan_root(conn, HOST, tree, hash_kind=hashing.PARTIAL)
    assert first.hashed == 3 and first.reused_hashes == 0

    second = scan_root(conn, HOST, tree, hash_kind=hashing.PARTIAL)
    assert second.hashed == 0
    assert second.reused_hashes == 3


def test_rescan_rehashes_a_changed_file(conn, tree):
    scan_root(conn, HOST, tree, hash_kind=hashing.PARTIAL)
    target = tree / "a.txt"
    target.write_text("alpha changed")
    os.utime(target, (1, 1))  # force a different mtime

    stats = scan_root(conn, HOST, tree, hash_kind=hashing.PARTIAL)
    assert stats.hashed == 1
    assert stats.reused_hashes == 2


def test_deleted_files_are_pruned(conn, tree):
    scan_root(conn, HOST, tree)
    (tree / "a.txt").unlink()
    stats = scan_root(conn, HOST, tree)
    assert stats.pruned == 1
    assert "a.txt" not in paths_in(conn)


def test_max_hash_size_skips_big_files(conn, tree):
    scan_root(conn, HOST, tree, hash_kind=hashing.PARTIAL, max_hash_bytes=100)
    big = conn.execute("SELECT * FROM files WHERE name = 'big.bin'").fetchone()
    small = conn.execute("SELECT * FROM files WHERE name = 'a.txt'").fetchone()
    assert big["hash"] is None
    assert small["hash"] is not None


def test_symlinks_are_recorded_not_followed(conn, tree, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("should not be indexed")
    (tree / "link").symlink_to(outside)

    stats = scan_root(conn, HOST, tree)
    assert stats.links == 1
    assert "secret.txt" not in paths_in(conn)


def test_symlink_cycle_terminates(conn, tree):
    (tree / "loop").symlink_to(tree)
    stats = scan_root(conn, HOST, tree)  # must not hang
    assert stats.links == 1


def test_credential_paths_are_skipped_and_counted(conn, tmp_path):
    root = tmp_path / "home"
    (root / ".ssh").mkdir(parents=True)
    (root / ".ssh" / "id_rsa").write_text("PRIVATE KEY")
    (root / "notes.txt").write_text("fine")

    ex = Excluder()
    scan_root(conn, HOST, root, excluder=ex)

    assert paths_in(conn) == {"notes.txt"}
    assert ex.skipped_credentials == 1
    assert "credential" in " ".join(ex.report())


def test_user_exclude_glob(conn, tree):
    ex = Excluder(extra=("*.bin",))
    scan_root(conn, HOST, tree, excluder=ex)
    assert "big.bin" not in paths_in(conn)
    assert ex.skipped_user == 1


def test_scanning_a_file_is_an_error(conn, tree):
    with pytest.raises(NotADirectoryError):
        scan_root(conn, HOST, tree / "a.txt")
