"""Last access time: recorded where it means something, never polluted by cdm itself.

The mount table is injected, so these behave the same on every CI runner
whatever /tmp is mounted with.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime

import pytest

from cdm import atime, cli, db, hashing, query, shape, suggest
from cdm import scan as scan_mod
from cdm.scan import scan_root

HOST = "testhost"
DAY = 86400
NOW = time.time()
# The real class, kept before the fixture below replaces the module attribute.
RealTrust = atime.Trust


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "index.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def trusted(monkeypatch):
    """Every filesystem updates access times unless a test says otherwise."""
    monkeypatch.setattr(scan_mod.atime_mod, "Trust",
                        lambda **_: RealTrust(table=[("/", {"rw", "relatime"})],
                                              platform="linux"))


def touch(path, *, atime_, mtime_):
    os.utime(path, (atime_, mtime_))


@pytest.fixture()
def reads_move_atime(monkeypatch):
    """Make every cdm read move the file's atime, as many filesystems do.

    Without this the tests pass trivially on a filesystem (or with O_NOATIME)
    where cdm's reads leave the atime alone, and prove nothing.
    """
    def bumping(real):
        def wrapped(path, *args):
            out = real(path, *args)
            st = os.stat(path)
            os.utime(path, (time.time(), st.st_mtime))
            return out
        return wrapped
    monkeypatch.setattr(hashing, "compute", bumping(hashing.compute))
    monkeypatch.setattr(hashing, "full_hash", bumping(hashing.full_hash))


def row(conn, path):
    return conn.execute("SELECT * FROM files WHERE path = ?",
                        (str(path.resolve()),)).fetchone()


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "home"
    (root / "d").mkdir(parents=True)
    f = root / "d" / "f.bin"
    f.write_bytes(b"x" * 5000)
    touch(f, atime_=NOW - 50 * DAY, mtime_=NOW - 60 * DAY)
    return root, f


# --- what is recorded ---------------------------------------------------------

def test_files_get_their_access_time_and_directories_do_not(conn, tree):
    root, f = tree
    scan_root(conn, HOST, root)
    assert row(conn, f)["atime"] == pytest.approx(NOW - 50 * DAY, abs=1)
    assert row(conn, root / "d")["atime"] is None


def test_a_noatime_filesystem_records_unknown_not_a_date(conn, tree, monkeypatch):
    root, f = tree
    point = str(root.resolve())
    monkeypatch.setattr(scan_mod.atime_mod, "Trust",
                        lambda **_: RealTrust(table=[(point, {"rw", "noatime"})],
                                              platform="linux"))
    scan_root(conn, HOST, root)
    assert row(conn, f)["atime"] is None


def test_trust_is_decided_by_device_then_path(tmp_path):
    here = str(tmp_path.resolve())
    dev = os.stat(here).st_dev
    linux = {"platform": "linux"}
    assert RealTrust(table=[(here, {"ro"})], **linux).check(here + "/x", dev)[0] is False
    assert RealTrust(table=[(here, {"rw"})], **linux).check(here + "/x", dev)[0] is True
    # No mount table at all: unverifiable, so not trusted.
    assert RealTrust(table=[], **linux).check(here, dev)[0] is False


def test_macos_is_trusted_only_where_measured(tmp_path):
    """APFS may move atime only on the first read after a change; options can't tell."""
    here = str(tmp_path.resolve())
    dev = os.stat(here).st_dev
    table = [(here, {"apfs", "local"})]
    assert RealTrust(table=table, platform="darwin").check(here, dev)[0] is False
    assert RealTrust(table=table, platform="darwin",
                     measured=(dev, True)).check(here, dev)[0] is True
    # A measurement beats the mount options, both ways.
    assert RealTrust(table=[(here, {"rw"})], platform="linux",
                     measured=(dev, False)).check(here, dev)[0] is False


def test_the_probe_measures_and_leaves_nothing_behind(tmp_path):
    result = atime.probe(tmp_path / "data")
    assert result is not None
    dev, moves = result
    assert dev == os.stat(tmp_path).st_dev and isinstance(moves, bool)
    assert list((tmp_path / "data").iterdir()) == []


# --- cdm's own reads ----------------------------------------------------------

def test_hashing_a_file_does_not_make_it_look_read(conn, tree, reads_move_atime):
    """cdm's read moved the atime; it must still not count as use."""
    root, f = tree
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    first = row(conn, f)
    assert first["hash"] and first["atime"] == pytest.approx(NOW - 50 * DAY, abs=1)
    assert first["self_atime"] is not None

    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    assert row(conn, f)["atime"] == pytest.approx(NOW - 50 * DAY, abs=1)


def test_a_real_read_after_cdms_is_recorded(conn, tree, reads_move_atime):
    root, f = tree
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    someone = NOW - 2 * DAY
    touch(f, atime_=someone, mtime_=NOW - 60 * DAY)
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    assert row(conn, f)["atime"] == pytest.approx(someone, abs=1)


def test_reads_by_an_earlier_hashing_scan_are_unknown_not_recent(conn, tree):
    """An index hashed before atime was recorded: its reads look like use."""
    root, f = tree
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    # Rewind to what an upgraded index holds: a hash, no atime bookkeeping,
    # and an atime left by that earlier scan's read.
    ours = NOW - 3 * DAY
    conn.execute("UPDATE files SET atime = NULL, self_atime = NULL")
    conn.execute("DELETE FROM scans")
    conn.execute("INSERT INTO scans (host, root, scan_id, started_at, finished_at, "
                 "hash_kind) VALUES (?, ?, 'old', ?, ?, 'partial')",
                 (HOST, str(root.resolve()),
                  datetime.fromtimestamp(ours - 60).isoformat(),
                  datetime.fromtimestamp(ours + 60).isoformat()))
    conn.commit()
    touch(f, atime_=ours, mtime_=NOW - 60 * DAY)

    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    assert row(conn, f)["atime"] is None, "cdm's old read was taken for use"
    assert row(conn, f)["self_atime"] == pytest.approx(ours, abs=1)

    later = NOW - DAY
    touch(f, atime_=later, mtime_=NOW - 60 * DAY)
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    assert row(conn, f)["atime"] == pytest.approx(later, abs=1)


def test_dupes_verify_does_not_make_files_look_read(tmp_path, monkeypatch, capsys,
                                                   reads_move_atime):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", HOST)
    root = tmp_path / "home"
    root.mkdir()
    a, b = root / "a.bin", root / "b.bin"
    for p in (a, b):
        p.write_bytes(b"same" * 1000)
        touch(p, atime_=NOW - 40 * DAY, mtime_=NOW - 60 * DAY)
    assert cli.main(["scan", str(root), "--checksum"]) == 0
    assert cli.main(["dupes", "--verify"]) == 0
    assert cli.main(["rescan", "--checksum"]) == 0
    capsys.readouterr()
    with sqlite3.connect(tmp_path / "data" / "index.db") as c:
        for p in (a, b):
            got = c.execute("SELECT atime FROM files WHERE path = ?",
                            (str(p.resolve()),)).fetchone()[0]
            assert got == pytest.approx(NOW - 40 * DAY, abs=1)


# --- using it -----------------------------------------------------------------

def test_find_by_access_time_never_matches_an_unknown(conn, tree):
    root, f = tree
    (root / "d" / "fresh.txt").write_text("x")
    scan_root(conn, HOST, root)
    old = query.find(conn, accessed_before=NOW - 30 * DAY, kind=None)
    assert [r["name"] for r in old] == ["f.bin"]
    recent = query.find(conn, accessed_after=NOW - DAY)
    assert [r["name"] for r in recent] == ["fresh.txt"]   # directories have none


def test_age_histogram_by_atime_counts_unknowns_separately(conn, tree, monkeypatch):
    root, f = tree
    scan_root(conn, HOST, root)
    conn.execute("UPDATE files SET atime = NULL WHERE name = 'f.bin'")
    (root / "d" / "g.bin").write_bytes(b"y")
    scan_root(conn, HOST, root)
    conn.execute("UPDATE files SET atime = NULL WHERE name = 'f.bin'")
    out = shape.age_histogram(conn, host=HOST, by="atime")
    assert out["measured_from"] == "atime"
    assert out["total_files"] == 1
    assert out["without_atime"]["files"] == 1
    with pytest.raises(ValueError):
        shape.age_histogram(conn, host=HOST, by="ctime")


def test_suggest_leaves_alone_what_is_still_being_read(conn, tmp_path, monkeypatch):
    """Old node_modules that the app still loads every day is not stale."""
    root = tmp_path / "home"
    (root / "proj" / ".git").mkdir(parents=True)
    (root / "proj" / "package.json").write_text("{}")
    dep = root / "proj" / "node_modules" / "lib.js"
    dep.parent.mkdir()
    dep.write_bytes(b"x" * 400)
    touch(dep, atime_=NOW - DAY, mtime_=NOW - 400 * DAY)
    scan_root(conn, HOST, root)
    ids = [s["id"] for s in suggest.suggest(conn, host=HOST, now=NOW)["suggestions"]]
    assert "build:rebuildable" not in ids

    touch(dep, atime_=NOW - 300 * DAY, mtime_=NOW - 400 * DAY)
    scan_root(conn, HOST, root)
    found = suggest.suggest(conn, host=HOST, now=NOW)["suggestions"]
    [s] = [s for s in found if s["id"] == "build:rebuildable"]
    assert s["items"][0]["last_read"].startswith(
        datetime.fromtimestamp(NOW - 300 * DAY).strftime("%Y-%m-%d"))


def test_cli_stat_and_doctor_show_access_times(tmp_path, monkeypatch, capsys, tree):
    root, f = tree
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", HOST)
    assert cli.main(["scan", str(root)]) == 0
    capsys.readouterr()
    assert cli.main(["stat", str(f)]) == 0
    assert "accessed  " in capsys.readouterr().out
    cli.main(["doctor"])
    assert "1 of 1 files have a trusted last-access time" in capsys.readouterr().out


# --- the upgrade --------------------------------------------------------------

def test_a_schema_3_index_gains_the_columns_and_keeps_its_rows(tmp_path, tree):
    root, f = tree
    path = tmp_path / "old.db"
    c = db.connect(path)
    scan_root(c, HOST, root, hash_kind=hashing.PARTIAL)
    try:
        c.execute("DROP INDEX idx_files_atime")
        c.execute("ALTER TABLE files DROP COLUMN atime")
        c.execute("ALTER TABLE files DROP COLUMN self_atime")
    except sqlite3.OperationalError:
        pytest.skip("this SQLite cannot DROP COLUMN to build a schema-3 index")
    c.execute("PRAGMA user_version = 3")
    c.commit()
    c.close()

    c = db.connect(path)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(files)")}
        assert {"atime", "self_atime"} <= cols
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert row(c, f)["hash"], "the upgrade lost data"
    finally:
        c.close()
