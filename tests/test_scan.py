"""Scanner behaviour: what it records, what it refuses, what it reuses."""
from __future__ import annotations

import os
import subprocess
import sys
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


# --- the index inside the tree it indexes ----------------------------------

def test_the_index_own_files_are_indexed_but_never_opened(tree, monkeypatch):
    """Scanning $HOME walks into the index. It must not open it to hash it."""
    c = db.connect(tree / "index.db")
    own = {str((tree / n).resolve()) for n in ("index.db", "index.db-wal", "index.db-shm")}
    opened = []
    real = hashing.compute
    monkeypatch.setattr(hashing, "compute",
                        lambda path, size, kind: opened.append(str(path)) or
                        real(path, size, kind))
    try:
        scan_root(c, HOST, tree, hash_kind=hashing.PARTIAL)
        assert not own & set(opened)
        rows = {r["name"]: r["hash"] for r in c.execute(
            "SELECT name, hash FROM files WHERE name LIKE 'index.db%'")}
        assert "index.db" in rows and rows["index.db"] is None
        assert opened, "nothing else was hashed either; the test proves nothing"
    finally:
        c.close()


def test_a_reader_cannot_delete_the_wal_out_from_under_a_scan(tree):
    """The crash this guards against, reproduced without the crash.

    Opening and closing index.db-shm drops the scanner's POSIX locks on it. The
    next connection to close then believes it is the last one, checkpoints, and
    deletes -wal and -shm while the scanner still has them mapped -- which is
    a SIGBUS in walFindFrame on its next read.
    """
    c = db.connect(tree / "index.db")
    try:
        scan_root(c, HOST, tree, hash_kind=hashing.PARTIAL)
        # Any other cdm command -- `du`, `doctor`, a second scan -- in ANOTHER
        # process: POSIX locks are per-process, so an in-process connection
        # would share the scanner's locks and prove nothing. Read-write,
        # because only a writable connection checkpoints and cleans up on close.
        subprocess.run(
            [sys.executable, "-c",
             "import sys; from cdm import db; c = db.connect(sys.argv[1]); "
             "c.execute('SELECT COUNT(*) FROM files').fetchone(); c.close()",
             str(tree / "index.db")],
            check=True, timeout=60)
        assert (tree / "index.db-wal").exists()
        assert (tree / "index.db-shm").exists()
        c.execute("SELECT COUNT(*) FROM files").fetchone()
    finally:
        c.close()


# --- nested roots ----------------------------------------------------------
#
# `~` and `~/src` both registered. files is keyed by (host, path), so each path
# has one row; it must have one owner, the most specific root, whichever scan
# wrote it last. "Whoever scanned last" made every scan steal the other root's
# rows and re-hash everything: observed as "hashed 95357, reused 0".

@pytest.fixture()
def home(tmp_path):
    outer = tmp_path / "home"
    (outer / "src" / "proj").mkdir(parents=True)
    (outer / "notes.txt").write_text("notes")
    (outer / "src" / "a.py").write_text("print(1)")
    (outer / "src" / "proj" / "b.py").write_text("print(2)")
    return outer.resolve(), (outer / "src").resolve()


def owner_of(conn, path):
    return conn.execute("SELECT root FROM files WHERE path = ?",
                        (str(path),)).fetchone()["root"]


@pytest.mark.parametrize("order", ["outer-first", "inner-first"])
def test_rescanning_either_root_reuses_the_others_hashes(conn, home, order):
    outer, inner = home
    first, second = (outer, inner) if order == "outer-first" else (inner, outer)
    scan_root(conn, HOST, first, hash_kind=hashing.PARTIAL)
    scan_root(conn, HOST, second, hash_kind=hashing.PARTIAL)

    for root in (outer, inner, outer, inner):
        stats = scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
        assert stats.hashed == 0, root
        assert stats.reused_hashes == stats.files


def test_rows_belong_to_the_most_specific_root_whoever_scanned_last(conn, home):
    outer, inner = home
    scan_root(conn, HOST, inner)
    scan_root(conn, HOST, outer)
    assert owner_of(conn, inner / "proj" / "b.py") == str(inner)
    assert owner_of(conn, inner / "a.py") == str(inner)
    # The nested root's own directory entry is a child of the outer root; the
    # nested scan never writes it, so there is nothing to fight over.
    assert owner_of(conn, inner) == str(outer)
    assert owner_of(conn, outer / "notes.txt") == str(outer)

    scan_root(conn, HOST, inner)
    assert owner_of(conn, inner / "a.py") == str(inner)


def test_the_first_scan_of_a_nested_root_claims_its_rows(conn, home):
    outer, inner = home
    scan_root(conn, HOST, outer)
    assert owner_of(conn, inner / "a.py") == str(outer)
    scan_root(conn, HOST, inner)
    assert owner_of(conn, inner / "a.py") == str(inner)


def test_outer_scan_prunes_deleted_files_inside_a_nested_root(conn, home):
    outer, inner = home
    scan_root(conn, HOST, outer)
    scan_root(conn, HOST, inner)
    (inner / "proj" / "b.py").unlink()

    stats = scan_root(conn, HOST, outer)
    assert stats.pruned == 1
    assert "b.py" not in paths_in(conn)


def test_inner_scan_does_not_prune_the_outer_roots_rows(conn, home):
    outer, inner = home
    scan_root(conn, HOST, outer)
    scan_root(conn, HOST, inner)
    stats = scan_root(conn, HOST, inner)
    assert stats.pruned == 0
    assert "notes.txt" in paths_in(conn)


def test_resume_descends_into_a_nested_root(conn, home):
    """A completed directory's children are found even when a nested root owns them.

    Looking them up by the scanning root alone found none, so the resumed walk
    silently stopped at the nested root and never re-read what lay below it.
    """
    outer, inner = home
    scan_root(conn, HOST, inner)
    scan_root(conn, HOST, outer)
    stamp = conn.execute("SELECT last_scan FROM roots WHERE path = ?",
                         (str(outer),)).fetchone()[0]
    conn.execute(
        "INSERT INTO scans (host, root, scan_id, started_at, hash_kind) "
        "VALUES (?,?,?,?,?)", (HOST, str(outer), "nest01", stamp, None))
    conn.executemany(
        "INSERT INTO scan_dirs (host, root, scan_id, path) VALUES (?,?,?,?)",
        [(HOST, str(outer), "nest01", str(p)) for p in (outer, inner)])
    conn.commit()

    stats = scan_root(conn, HOST, outer, resume=True, now=stamp)
    assert stats.resumed_from == 2
    assert stats.files == 1        # proj/b.py: proj was not checkpointed
    assert "b.py" in paths_in(conn)
