"""`cdm hash`: hashing imported (and scanned) files that could be duplicates.

The listings here describe real files in a temporary directory, so hashing
reads genuine files while the rows are genuinely imported.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from test_policy import HEADER, line

from cdm import cli, db, guide, hasher, hashing, importer, policy, query, roots, suggest

LOCAL = "laptop"
HOST = "fs1@cluster1.example"
DAY = 86400


def utc(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "index.db")
    yield c
    c.close()


@pytest.fixture()
def tree(tmp_path):
    """Files on disk: a duplicate pair, a same-size non-duplicate, a unique size."""
    root = tmp_path / "fs1" / "proj"
    root.mkdir(parents=True)
    t = 1_780_000_000.0                  # whole microseconds, as listings report
    files = {"dup1.bin": b"A" * 1000, "dup2.bin": b"A" * 1000, "other.bin": b"B" * 1000,
             "unique.bin": b"C" * 77}
    for name, data in files.items():
        p = root / name
        p.write_bytes(data)
        os.utime(p, (t - 10 * DAY, t))
    return root


def listing_for(root, *, extra=(), atime_of=None):
    rows = []
    for p in sorted(root.iterdir()):
        st = p.stat()
        a = atime_of(p) if atime_of else st.st_atime
        rows.append(line(str(p), inode=st.st_ino, size=st.st_size, mtime=utc(st.st_mtime),
                         atime=utc(a), fileset="proj"))
    rows += list(extra)
    head = [h.replace("# scope: fileset proj", "# scope: fileset proj") for h in HEADER]
    return head + rows + [f"{policy.END}{len(rows)}\n"]


def imported(conn, tmp_path, root, name="l.raw", **kw):
    p = tmp_path / name
    p.write_text("".join(listing_for(root, **kw)))
    importer.import_policy(conn, p, root=str(root))
    return roots.visible_hosts(conn, LOCAL)


def hashes(conn):
    return {os.path.basename(r[0]): r[1] for r in conn.execute(
        "SELECT path, hash FROM files WHERE type = 'file'")}


def test_only_size_matched_files_are_read(conn, tmp_path, tree):
    hosts = imported(conn, tmp_path, tree)
    assert hasher.candidates(conn, hosts) == 3
    stats = hasher.hash_files(conn, hosts)
    assert (stats.hashed, stats.candidates) == (3, 3)
    h = hashes(conn)
    assert h["unique.bin"] is None, "a file of a size no other file has was read"
    assert h["dup1.bin"] == h["dup2.bin"] != h["other.bin"]


def test_dupes_works_on_imported_data_after_hashing(conn, tmp_path, tree):
    hosts = imported(conn, tmp_path, tree)
    assert query.dupe_groups(conn, host=hosts) == []
    hasher.hash_files(conn, hosts)
    [group] = query.dupe_groups(conn, host=hosts)
    assert {os.path.basename(m["path"]) for m in group["members"]} == {"dup1.bin",
                                                                       "dup2.bin"}


def test_a_rerun_resumes_and_does_nothing_twice(conn, tmp_path, tree):
    hosts = imported(conn, tmp_path, tree)
    hasher.hash_files(conn, hosts)
    again = hasher.hash_files(conn, hosts)
    assert (again.candidates, again.hashed) == (0, 0)


def test_a_file_changed_since_listing_is_not_hashed(conn, tmp_path, tree):
    hosts = imported(conn, tmp_path, tree)
    (tree / "dup2.bin").write_bytes(b"Z" * 1000)          # same size, new mtime
    stats = hasher.hash_files(conn, hosts)
    assert stats.changed == 1 and hashes(conn)["dup2.bin"] is None


def test_files_not_on_this_machine_are_counted_not_errors(conn, tmp_path, tree):
    ghost = line(f"{tree}/ghost.bin", size=1000, fileset="proj")
    hosts = imported(conn, tmp_path, tree, extra=[ghost])
    stats = hasher.hash_files(conn, hosts)
    assert stats.elsewhere == 1 and stats.hashed == 3 and not stats.unreadable


def test_scope_by_root_fileset_and_size(conn, tmp_path, tree):
    hosts = imported(conn, tmp_path, tree)
    assert hasher.candidates(conn, hosts, fileset="other") == 0
    assert hasher.candidates(conn, hosts, root=str(tree / "nowhere")) == 0
    assert hasher.candidates(conn, hosts, min_size=2000) == 0
    assert hasher.candidates(conn, hosts, root=str(tree), fileset="proj") == 3


def test_full_hashes_are_their_own_kind(conn, tmp_path, tree):
    hosts = imported(conn, tmp_path, tree)
    hasher.hash_files(conn, hosts)
    assert hasher.candidates(conn, hosts, kind=hashing.FULL) == 3
    hasher.hash_files(conn, hosts, kind=hashing.FULL)
    kinds = {r[0] for r in conn.execute("SELECT hash_kind FROM files WHERE hash IS NOT NULL")}
    assert kinds == {hashing.FULL}


def test_unreadable_files_are_reported(conn, tmp_path, tree):
    hosts = imported(conn, tmp_path, tree)
    os.chmod(tree / "dup1.bin", 0)
    try:
        if os.access(tree / "dup1.bin", os.R_OK):
            pytest.skip("running as a user who can read anything")
        stats = hasher.hash_files(conn, hosts)
    finally:
        os.chmod(tree / "dup1.bin", 0o644)
    assert [os.path.basename(p) for p in stats.unreadable] == ["dup1.bin"]


# --- cdm's own read is not use -------------------------------------------------

def bump_on_read(monkeypatch):
    """Make cdm's hashing read move the atime, as it does without O_NOATIME."""
    real = hashing.compute

    def reading(path, *args):
        out = real(path, *args)
        st = os.stat(path)
        os.utime(path, ns=(st.st_mtime_ns + 5 * DAY * 10**9 + 123, st.st_mtime_ns))
        return out
    monkeypatch.setattr(hashing, "compute", reading)


def atime_of(conn, name):
    return conn.execute("SELECT atime FROM files WHERE path LIKE ?",
                        (f"%/{name}",)).fetchone()[0]


def test_a_re_import_after_hashing_keeps_the_earlier_last_read(conn, tmp_path, tree,
                                                              monkeypatch):
    bump_on_read(monkeypatch)
    hosts = imported(conn, tmp_path, tree, "1.raw")
    before = atime_of(conn, "dup1.bin")
    hasher.hash_files(conn, hosts)
    # The next listing shows the atime cdm's read left -- to the microsecond.
    imported(conn, tmp_path, tree, "2.raw")
    assert atime_of(conn, "dup1.bin") == before, "cdm's own read was taken for use"


def test_a_real_read_after_hashing_is_recorded(conn, tmp_path, tree, monkeypatch):
    bump_on_read(monkeypatch)
    hosts = imported(conn, tmp_path, tree, "1.raw")
    hasher.hash_files(conn, hosts)
    someone = 1_780_000_000.0 + 20 * DAY
    imported(conn, tmp_path, tree, "2.raw",
             atime_of=lambda p: someone if p.name == "dup1.bin" else p.stat().st_atime)
    assert atime_of(conn, "dup1.bin") == someone


# --- what points at it ------------------------------------------------------------

def test_guide_and_suggest_point_at_cdm_hash_until_it_is_done(conn, tmp_path, tree):
    hosts = imported(conn, tmp_path, tree)
    ids = [s["id"] for s in suggest.suggest(conn, host=hosts)["suggestions"]]
    assert any(i.startswith("index.unhashed:") for i in ids)
    step = next(s for s in guide.guide(conn, host=hosts)["steps"] if s["id"] == "hash")
    assert step["status"] == guide.TODO and step["command"].startswith("cdm hash --root")
    hasher.hash_files(conn, hosts)
    ids = [s["id"] for s in suggest.suggest(conn, host=hosts)["suggestions"]]
    assert not any(i.startswith("index.unhashed:") for i in ids)
    step = next(s for s in guide.guide(conn, host=hosts)["steps"] if s["id"] == "hash")
    assert step["status"] == guide.DONE


def test_cdm_hash_command(tmp_path, tree, monkeypatch, capsys):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", LOCAL)
    p = tmp_path / "l.raw"
    p.write_text("".join(listing_for(tree)))
    assert cli.main(["import", "--policy", str(p), "--root", str(tree)]) == 0
    capsys.readouterr()
    assert cli.main(["hash"]) == 0
    assert "hashed 3 of 3 size-matched file(s)" in capsys.readouterr().err
    assert cli.main(["dupes"]) == 0
    assert "dup1.bin" in capsys.readouterr().out
