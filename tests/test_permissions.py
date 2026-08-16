"""The privacy promise, tested rather than asserted in a README.

An index of every filename you own is more revealing than most file contents,
and this tool's whole pitch is that it stays local and private. These tests
exist because that promise previously held by accident.
"""
from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from cdm import cli, db, paths


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture()
def permissive_umask():
    """Most systems default to 022, which makes new files 0644."""
    old = os.umask(0o022)
    yield
    os.umask(old)


def test_index_is_0600_under_a_permissive_umask(tmp_path, permissive_umask):
    conn = db.connect(tmp_path / "index.db")
    conn.execute("INSERT INTO roots (host, path, added_at) VALUES ('h','/p','now')")
    conn.commit()
    conn.close()
    assert mode_of(tmp_path / "index.db") == 0o600


def test_wal_and_shm_are_not_world_readable(tmp_path, permissive_umask):
    """The regression that motivated this file.

    SQLite copies the main database's permissions onto -wal and -shm when it
    creates them, so enabling WAL before fixing the mode leaves both at 0644 --
    holding the same filename data the index protects. Two lines in the wrong
    order and this leaks silently.
    """
    conn = db.connect(tmp_path / "index.db")
    conn.execute("INSERT INTO roots (host, path, added_at) VALUES ('h','/p','now')")
    conn.commit()

    for suffix in ("-wal", "-shm"):
        sidecar = tmp_path / f"index.db{suffix}"
        if sidecar.exists():
            assert not mode_of(sidecar) & 0o077, (
                f"index.db{suffix} is {oct(mode_of(sidecar))} and readable by "
                f"other users")
    conn.close()


def test_the_ordering_this_depends_on_is_real(tmp_path, permissive_umask):
    """Prove the guard above is guarding something.

    If SQLite ever stopped inheriting the mode, the test above would pass for
    the wrong reason and the comment in db.py would be folklore. This asserts
    the failure mode still exists when the order is wrong.
    """
    path = tmp_path / "wrong-order.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")     # before the chmod, deliberately
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    os.chmod(path, 0o600)
    conn.commit()
    conn.close()

    wal = tmp_path / "wrong-order.db-wal"
    if wal.exists():
        assert mode_of(wal) & 0o077, (
            "SQLite no longer inherits the database mode onto -wal; the "
            "ordering comment in db.py can be simplified")


def test_data_dir_is_0700(tmp_path, monkeypatch, permissive_umask):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    paths.ensure_data_dir()
    assert mode_of(paths.data_dir()) == 0o700


def test_enforce_mode_reports_a_mode_it_could_not_fix(tmp_path, monkeypatch):
    """chmod failing must produce a warning, not silence."""
    target = tmp_path / "f"
    target.write_text("x")
    os.chmod(target, 0o644)

    def refuse(*_args, **_kwargs):
        raise OSError("filesystem does not support chmod")

    monkeypatch.setattr(paths.os, "chmod", refuse)
    warning = paths.enforce_mode(target, 0o600)
    assert warning is not None
    assert "0o644" in warning and "read it" in warning


def test_enforce_mode_is_quiet_when_the_mode_holds(tmp_path):
    target = tmp_path / "f"
    target.write_text("x")
    assert paths.enforce_mode(target, 0o600) is None


def test_warning_goes_to_stderr(tmp_path, capsys):
    paths.warn("cdm: WARNING something")
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert captured.out == ""


# --- doctor must fail, not just mention it ---------------------------------

def test_doctor_exits_nonzero_on_a_readable_index(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", "testhost")
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f.txt").write_text("x")
    cli.main(["scan", str(root)])
    capsys.readouterr()

    os.chmod(paths.index_path(), 0o644)
    assert cli.main(["doctor"]) == 1
    assert "OTHERS CAN READ" in capsys.readouterr().out


def test_doctor_permission_failure_survives_later_clean_checks(
        tmp_path, monkeypatch, capsys):
    """A regression: rc was reset after the roots loop, discarding the finding."""
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", "testhost")
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f.txt").write_text("x")
    cli.main(["scan", str(root)])       # leaves one healthy, present root
    capsys.readouterr()

    os.chmod(paths.index_path(), 0o604)
    assert cli.main(["doctor"]) == 1, "permission finding was overwritten"


def test_doctor_is_clean_on_a_healthy_index(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", "testhost")
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f.txt").write_text("x")
    cli.main(["scan", str(root)])
    capsys.readouterr()
    assert cli.main(["doctor"]) == 0
    assert "OTHERS CAN READ" not in capsys.readouterr().out


# --- verification must not silently drop files -----------------------------

def test_verify_reports_files_it_could_not_read(tmp_path, monkeypatch, capsys):
    from cdm import hashing, query
    from cdm.scan import scan_root

    monkeypatch.setenv("CDM_HOST", "testhost")
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"z" * 5000)
    (root / "b.bin").write_bytes(b"z" * 5000)

    conn = db.connect(tmp_path / "index.db")
    scan_root(conn, "testhost", root, hash_kind=hashing.PARTIAL)
    group = query.dupe_groups(conn)[0]

    def refuse(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(query.hashing, "full_hash", refuse)
    confirmed, unreadable = query.verify_group(group)
    conn.close()

    assert confirmed == []
    assert len(unreadable) == 2, "unreadable files were silently dropped"


def test_cli_dupes_verify_exits_nonzero_when_files_are_unreadable(
        tmp_path, monkeypatch, capsys):
    from cdm import query

    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", "testhost")
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"z" * 5000)
    (root / "b.bin").write_bytes(b"z" * 5000)
    cli.main(["scan", str(root), "--checksum"])
    capsys.readouterr()

    def refuse(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(query.hashing, "full_hash", refuse)
    rc = cli.main(["dupes", "--verify"])
    assert rc == 1
    assert "could not be read" in capsys.readouterr().err
