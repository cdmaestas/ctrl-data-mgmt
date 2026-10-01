"""`cdm import --policy`, and imported listings being visible everywhere.

Uses the sanitized fixture from a real Storage Scale cluster, and synthetic
listings (assembled at run time) for the edge cases.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_policy import HEADER, line

from cdm import cli, db, guide, hashing, importer, policy, query, roots, shape, suggest, tools
from cdm.scan import scan_root

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "storage-scale-fileset.list"
LOCAL = "laptop"
IMPORTED = "fs1@cluster1.example"
ROOT = "/gpfs/fs1/fileset1"


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "index.db")
    yield c
    c.close()


def write(tmp_path, lines, name="x.list.raw"):
    p = tmp_path / name
    p.write_text("".join(lines))
    return p


def listing(*rows, end=True):
    return HEADER + list(rows) + ([f"{policy.END}{len(rows)}\n"] if end else [])


def count(conn, host=IMPORTED):
    return conn.execute("SELECT COUNT(*) FROM files WHERE host = ?", (host,)).fetchone()[0]


# --- importing ------------------------------------------------------------------

def test_the_real_fixture_imports_under_a_logical_host(conn):
    stats = importer.import_policy(conn, FIXTURE)
    assert (stats.host, stats.root) == (IMPORTED, ROOT)
    assert (stats.files, stats.dirs, stats.links) == (13, 10, 1)
    assert count(conn) == 24, "the root records its contents, not itself"
    r = conn.execute("SELECT * FROM roots").fetchone()
    assert (r["host"], r["path"], r["source"]) == (IMPORTED, ROOT, roots.POLICY)
    f = query.stat_one(conn, f"{ROOT}/d0001/d0008/f0011.dmg", host=IMPORTED)
    assert f["size"] == 524_288_000 and f["type"] == "file" and f["root"] == ROOT


def test_an_incomplete_listing_is_refused_and_changes_nothing(conn, tmp_path):
    p = write(tmp_path, listing(line(f"{ROOT}/a"), line(f"{ROOT}/b"), end=False))
    with pytest.raises(policy.ListingError, match="incomplete"):
        importer.import_policy(conn, p)
    assert count(conn) == 0 and not conn.execute("SELECT * FROM roots").fetchall()


def test_a_damaged_line_part_way_changes_nothing(conn, tmp_path):
    rows = [line(f"{ROOT}/a"), "not a listing line\n", line(f"{ROOT}/b")]
    with pytest.raises(policy.ListingError):
        importer.import_policy(conn, write(tmp_path, listing(*rows)))
    assert count(conn) == 0


def test_a_file_that_is_not_a_listing_is_refused(conn, tmp_path):
    with pytest.raises(policy.ListingError, match="cdm policy"):
        importer.import_policy(conn, write(tmp_path, ["hello\n"]))


def test_the_root_can_be_given_and_nothing_may_lie_outside_it(conn, tmp_path):
    p = write(tmp_path, listing(line(f"{ROOT}/d/a"), line("/gpfs/fs1/elsewhere/b")))
    with pytest.raises(policy.ListingError, match="outside the root"):
        importer.import_policy(conn, p, root=f"{ROOT}")
    assert count(conn) == 0
    stats = importer.import_policy(conn, p, root="/gpfs/fs1")
    assert stats.root == "/gpfs/fs1" and count(conn) == 2


def test_a_single_file_listing_is_rooted_at_its_directory(conn, tmp_path):
    stats = importer.import_policy(conn, write(tmp_path, listing(line(f"{ROOT}/only"))))
    assert stats.root == ROOT


def test_reimport_updates_and_keeps_hashes_that_still_apply(conn, tmp_path):
    importer.import_policy(conn, FIXTURE)
    a, b = f"{ROOT}/d0001/d0002/d0004/f0008.gguf", f"{ROOT}/d0001/d0002/d0004/f0009.gguf"
    for p in (a, b):
        conn.execute("UPDATE files SET hash='h', hash_kind='partial', hash_size=size, "
                     "hash_mtime=mtime WHERE path = ?", (p,))
    conn.commit()
    # A re-import in which one of the two files changed size.
    text = FIXTURE.read_text().replace("2147483648%7C2026-08-29%2021%3A25", "2147483649%7C"
                                       "2026-08-29%2021%3A25")
    assert text != FIXTURE.read_text()
    importer.import_policy(conn, write(tmp_path, [text], "again.list.raw"))
    hashes = dict(conn.execute("SELECT path, hash FROM files WHERE path IN (?, ?)", (a, b)))
    assert hashes == {a: "h", b: None}
    assert count(conn) == 24, "re-importing duplicated rows"


# --- visibility -------------------------------------------------------------------

@pytest.fixture()
def mixed(tmp_path, conn):
    """A local scan, an imported listing, and a row merged from another machine."""
    local = tmp_path / "home"
    (local / "d").mkdir(parents=True)
    (local / "d" / "big.bin").write_bytes(b"x" * 5000)
    scan_root(conn, LOCAL, local, hash_kind=hashing.PARTIAL)
    importer.import_policy(conn, FIXTURE)
    conn.execute("INSERT INTO files (host, root, path, parent, name, size, mtime, ctime, "
                 "type, seen_at) VALUES ('other-box', '/r', '/r/secret', '/r', 'secret', "
                 "999999999, 0, 0, 'file', 'x')")
    conn.execute("INSERT INTO roots (host, path, added_at, last_scan) "
                 "VALUES ('other-box', '/r', 'x', 'x')")
    conn.commit()
    return local


def test_visible_hosts_are_this_machine_plus_imported_listings(conn, mixed):
    assert roots.visible_hosts(conn, LOCAL) == (LOCAL, IMPORTED)


def test_find_du_and_stat_see_local_and_imported_but_not_merged(conn, mixed):
    hosts = roots.visible_hosts(conn, LOCAL)
    found = {r["path"] for r in query.find(conn, host=hosts, kind="file")}
    assert f"{ROOT}/d0001/d0008/f0011.dmg" in found
    assert str((mixed / "d" / "big.bin").resolve()) in found
    assert "/r/secret" not in found
    assert query.disk_usage(conn, ROOT, host=hosts)
    assert query.stat_one(conn, f"{ROOT}/d0001/d0008/f0011.dmg", host=hosts)
    assert query.stat_one(conn, "/r/secret", host=hosts) is None


def test_summary_suggest_and_guide_see_imported_roots(conn, mixed):
    hosts = roots.visible_hosts(conn, LOCAL)
    listed = {r["root"] for r in shape.summary(conn, host=hosts)["roots"]}
    assert ROOT in listed and "/r" not in listed
    ids = [s["id"] for s in suggest.suggest(conn, host=hosts)["suggestions"]]
    assert "files:models" in ids and "files:installers" in ids
    g = guide.guide(conn, host=hosts)
    assert ROOT in next(s for s in g["steps"] if s["id"] == "index")["detail"]


def test_an_old_import_is_re_imported_not_rescanned(conn, mixed):
    import time
    found = suggest.suggest(conn, host=roots.visible_hosts(conn, LOCAL),
                            now=time.time() + 30 * 86400)["suggestions"]
    stale = [s for s in found if s["id"].startswith("index.stale")]
    reimport = next(s for s in stale if ROOT in s["title"])
    assert reimport["title"].startswith("Re-import") and "cdm policy" in reimport["action"]
    assert not any(s["id"] == f"index.unhashed:{ROOT}" for s in found)


def test_an_import_only_index_does_not_point_at_a_rescan(conn):
    importer.import_policy(conn, FIXTURE)
    g = guide.guide(conn, host=roots.visible_hosts(conn, LOCAL))
    step = next(s for s in g["steps"] if s["id"] == "hash")
    assert step["status"] == guide.OPTIONAL and g["next"] != "hash"


def test_mcp_tools_see_imported_listings(tmp_path):
    index = tmp_path / "index.db"
    c = db.connect(index)
    importer.import_policy(c, FIXTURE)
    c.close()
    cat = tools.Catalog(index=index, host=LOCAL, expose_names=True)
    assert cat.summary()["total_files"] == 13
    assert cat.find(larger_than="100M")["results"]
    assert cat.summary(root=ROOT)["roots"][0]["root"] == ROOT
    assert any("imported listing" in line for line in cat.banner(expose_names=False))


# --- the command ------------------------------------------------------------------

def test_cdm_import_and_the_commands_after_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", LOCAL)
    assert cli.main(["import", "--policy", str(FIXTURE)]) == 0
    assert "imported 13 files, 10 dirs, 1 links" in capsys.readouterr().err
    assert cli.main(["roots"]) == 0
    assert "imported listing" in capsys.readouterr().out
    assert cli.main(["find", "--larger-than", "100M", "--quiet"]) == 0
    assert len(capsys.readouterr().out.split()) == 3
    assert cli.main(["forget", ROOT]) == 0
    capsys.readouterr()
    assert cli.main(["roots"]) == 0
    assert ROOT not in capsys.readouterr().out


def test_cdm_import_of_a_truncated_listing_fails_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    p = write(tmp_path, listing(line(f"{ROOT}/a"), end=False))
    assert cli.main(["import", "--policy", str(p)]) == 1
    assert "nothing was imported" in capsys.readouterr().err


def test_a_schema_5_index_gains_roots_source(tmp_path):
    path = tmp_path / "old.db"
    c = db.connect(path)
    c.execute("INSERT INTO roots (host, path, added_at) VALUES ('h', '/r', 'x')")
    try:
        c.execute("ALTER TABLE roots DROP COLUMN source")
    except sqlite3.OperationalError:
        pytest.skip("this SQLite cannot DROP COLUMN to build a schema-5 index")
    c.execute("PRAGMA user_version = 5")
    c.commit()
    c.close()
    c = db.connect(path)
    try:
        assert "source" in {r[1] for r in c.execute("PRAGMA table_info(roots)")}
        assert c.execute("SELECT source FROM roots").fetchone()[0] is None
    finally:
        c.close()


def test_doctor_does_not_call_an_imported_root_gone(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["import", "--policy", str(FIXTURE)]) == 0
    capsys.readouterr()
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "imported listing" in out and "gone from disk" not in out
