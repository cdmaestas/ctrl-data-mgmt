"""Absence must be observed, never assumed.

A scan infers "this file is gone" from not having seen it. That inference is
only valid for directories it could actually read. These tests exist because it
was previously applied to directories it could not: a temporarily unreadable
tree -- an unmounted share, a permissions change, a revoked Full Disk Access on
macOS -- made a rescan delete every row under it and report them as "no longer
on disk", with exit status 0.
"""
from __future__ import annotations

import os

import pytest

from cdm import cli, db, hashing
from cdm.scan import scan_root

HOST = "testhost"


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "index.db")
    yield c
    c.close()


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "data"
    (root / "keep").mkdir(parents=True)
    (root / "locked").mkdir()
    for i in range(3):
        (root / "keep" / f"k{i}.txt").write_text(f"k{i}")
        (root / "locked" / f"l{i}.txt").write_text(f"l{i}")
    (root / "top.txt").write_text("top")
    return root


def row_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]


def names(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM files")}


@pytest.fixture()
def unreadable():
    """chmod 000 a directory, and always put it back."""
    restored = []

    def lock(path):
        restored.append((path, os.stat(path).st_mode))
        os.chmod(path, 0o000)
        return path

    yield lock
    for path, mode in restored:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_an_unreadable_subtree_is_not_pruned(conn, tree, unreadable):
    """The data-loss regression, at subtree granularity."""
    scan_root(conn, HOST, tree)
    before = row_count(conn)
    assert before == 9   # keep/ + locked/ + 3 + 3 files + top.txt
    unreadable(tree / "locked")

    stats = scan_root(conn, HOST, tree)
    assert stats.pruned == 0, "rows were pruned for a directory we could not read"
    assert {"l0.txt", "l1.txt", "l2.txt"} <= names(conn), (
        "files under an unreadable directory were deleted from the index")
    assert row_count(conn) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_an_unreadable_root_prunes_nothing(conn, tree, unreadable):
    scan_root(conn, HOST, tree)
    before = row_count(conn)
    unreadable(tree)

    stats = scan_root(conn, HOST, tree)
    assert stats.root_unreadable is True
    assert stats.pruned == 0
    assert row_count(conn) == before, "an unreadable root wiped the index"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_an_unreadable_root_does_not_get_a_last_scan_stamp(conn, tree, unreadable):
    """Otherwise 'I could not look' reads as 'I looked and it was empty'."""
    scan_root(conn, HOST, tree)
    first = conn.execute("SELECT last_scan FROM roots").fetchone()[0]
    unreadable(tree)

    scan_root(conn, HOST, tree)
    after = conn.execute("SELECT last_scan FROM roots").fetchone()[0]
    assert after == first, "a failed scan advanced the root's last_scan time"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_a_never_scanned_unreadable_root_is_not_recorded(conn, tmp_path, unreadable):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "f.txt").write_text("x")
    unreadable(locked)

    stats = scan_root(conn, HOST, locked)
    assert stats.root_unreadable is True
    assert conn.execute("SELECT COUNT(*) FROM roots").fetchone()[0] == 0, (
        "a root that could not be read was recorded as though it had been scanned")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_unreadable_directories_are_not_checkpointed(conn, tree, unreadable):
    """Otherwise a resumed scan skips them forever."""
    unreadable(tree / "locked")
    scan_root(conn, HOST, tree)
    # Checkpoints are cleared on success, so assert against a live scan instead:
    # the locked directory must not be treated as already-done on a resume.
    conn.execute(
        "INSERT INTO scans (host, root, scan_id, started_at, hash_kind) "
        "VALUES (?,?,?,?,?)", (HOST, str(tree), "open", "2020-01-01", None))
    conn.commit()
    done = {r[0] for r in conn.execute("SELECT path FROM scan_dirs")}
    assert str(tree / "locked") not in done


def test_deletions_in_readable_directories_are_still_pruned(conn, tree, unreadable):
    """The fix must not disable pruning altogether."""
    scan_root(conn, HOST, tree)
    (tree / "keep" / "k0.txt").unlink()

    stats = scan_root(conn, HOST, tree)
    assert stats.pruned == 1
    assert "k0.txt" not in names(conn)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_pruning_still_works_elsewhere_while_one_subtree_is_unreadable(
        conn, tree, unreadable):
    scan_root(conn, HOST, tree)
    unreadable(tree / "locked")
    (tree / "keep" / "k1.txt").unlink()

    stats = scan_root(conn, HOST, tree)
    assert stats.pruned == 1, "a readable directory's deletion was not noticed"
    assert "k1.txt" not in names(conn)
    assert "l0.txt" in names(conn)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_hashes_survive_an_unreadable_pass(conn, tree, unreadable):
    scan_root(conn, HOST, tree, hash_kind=hashing.PARTIAL)
    hashed = conn.execute(
        "SELECT COUNT(*) FROM files WHERE hash IS NOT NULL").fetchone()[0]
    unreadable(tree)

    scan_root(conn, HOST, tree, hash_kind=hashing.PARTIAL)
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE hash IS NOT NULL").fetchone()[0] == hashed


# --- CLI ------------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_cli_scan_of_an_unreadable_root_exits_nonzero(
        tmp_path, monkeypatch, capsys, unreadable):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", "testhost")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "f.txt").write_text("x")
    unreadable(locked)

    assert cli.main(["scan", str(locked)]) == 1
    err = capsys.readouterr().err
    assert "FAILED" in err
    assert "nothing was removed" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_cli_rescan_reports_failure_rather_than_zero_files(
        tmp_path, monkeypatch, capsys, unreadable):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", "testhost")
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f.txt").write_text("x")
    cli.main(["scan", str(root)])
    capsys.readouterr()

    unreadable(root)
    rc = cli.main(["rescan"])
    out = capsys.readouterr().err
    assert rc == 1
    assert "FAILED" in out
    # The old behaviour printed a reassuring "0 files, 0 dirs".
    assert "0 files" not in out


# --- case-insensitive matching --------------------------------------------

def test_iname_matches_regardless_of_case(tmp_path, monkeypatch, capsys):
    """APFS treats Report.TXT and report.txt as one name; GLOB does not."""
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", "testhost")
    root = tmp_path / "tree"
    root.mkdir()
    (root / "Report.TXT").write_text("a")
    (root / "notes.txt").write_text("b")
    cli.main(["scan", str(root)])
    capsys.readouterr()

    cli.main(["find", "--iname", "*.txt", "--quiet"])
    found = {line.rsplit("/", 1)[-1] for line in
             capsys.readouterr().out.strip().splitlines()}
    assert found == {"Report.TXT", "notes.txt"}


def test_name_stays_case_sensitive(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", "testhost")
    root = tmp_path / "tree"
    root.mkdir()
    (root / "Report.TXT").write_text("a")
    (root / "notes.txt").write_text("b")
    cli.main(["scan", str(root)])
    capsys.readouterr()

    cli.main(["find", "--name", "*.txt", "--quiet"])
    found = {line.rsplit("/", 1)[-1] for line in
             capsys.readouterr().out.strip().splitlines()}
    assert found == {"notes.txt"}
