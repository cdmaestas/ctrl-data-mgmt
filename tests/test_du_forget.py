"""Disk-usage rollup, root removal, and scan progress."""
from __future__ import annotations

import pytest

from cdm import db, paths, query
from cdm.scan import PROGRESS_EVERY, scan_root

HOST = "testhost"


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "index.db")
    yield c
    c.close()


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "data"
    (root / "big").mkdir(parents=True)
    (root / "small").mkdir()
    (root / "big" / "deep").mkdir()
    (root / "big" / "a.bin").write_bytes(b"x" * 3000)
    (root / "big" / "deep" / "b.bin").write_bytes(b"x" * 5000)
    (root / "small" / "c.txt").write_bytes(b"x" * 100)
    (root / "loose.txt").write_bytes(b"x" * 50)
    return root


# --- du --------------------------------------------------------------------

def test_du_rolls_up_by_subdirectory(conn, tree):
    scan_root(conn, HOST, tree)
    rows = {r["path"]: r for r in query.disk_usage(conn, str(tree), depth=1)}

    assert rows[str(tree / "big")]["bytes"] == 8000     # 3000 + nested 5000
    assert rows[str(tree / "big")]["files"] == 2
    assert rows[str(tree / "small")]["bytes"] == 100


def test_du_orders_biggest_first(conn, tree):
    scan_root(conn, HOST, tree)
    rows = query.disk_usage(conn, str(tree), depth=1)
    assert [r["bytes"] for r in rows] == sorted(
        (r["bytes"] for r in rows), reverse=True)


def test_du_reports_loose_files_at_their_own_path(conn, tree):
    scan_root(conn, HOST, tree)
    rows = {r["path"]: r for r in query.disk_usage(conn, str(tree), depth=1)}
    assert rows[str(tree / "loose.txt")]["bytes"] == 50


def test_du_depth_two_splits_the_nested_directory(conn, tree):
    scan_root(conn, HOST, tree)
    rows = {r["path"]: r for r in query.disk_usage(conn, str(tree), depth=2)}
    assert str(tree / "big" / "deep") in rows
    assert rows[str(tree / "big" / "deep")]["bytes"] == 5000


def test_du_excludes_directory_inodes_from_the_total(conn, tree):
    """A directory's own size is not the space its contents take."""
    scan_root(conn, HOST, tree)
    rows = query.disk_usage(conn, str(tree), depth=1)
    assert sum(r["bytes"] for r in rows) == 3000 + 5000 + 100 + 50


def test_du_of_an_unscanned_path_is_empty(conn, tree, tmp_path):
    scan_root(conn, HOST, tree)
    assert query.disk_usage(conn, str(tmp_path / "elsewhere")) == []


def test_du_prefix_does_not_leak_into_sibling_directories(conn, tmp_path):
    """`/data` must not swallow `/data-archive`."""
    a = tmp_path / "data"
    b = tmp_path / "data-archive"
    a.mkdir()
    b.mkdir()
    (a / "one.bin").write_bytes(b"x" * 10)
    (b / "two.bin").write_bytes(b"x" * 9999)
    scan_root(conn, HOST, a)
    scan_root(conn, HOST, b)

    rows = query.disk_usage(conn, str(a), depth=1)
    assert sum(r["bytes"] for r in rows) == 10


def test_du_handles_wildcard_characters_in_path(conn, tmp_path):
    """A directory called `100%_backup` is a path, not a LIKE pattern."""
    weird = tmp_path / "100%_backup"
    weird.mkdir()
    (weird / "f.bin").write_bytes(b"x" * 42)
    decoy = tmp_path / "1005Xbackup"
    decoy.mkdir()
    (decoy / "g.bin").write_bytes(b"x" * 777)
    scan_root(conn, HOST, weird)
    scan_root(conn, HOST, decoy)

    rows = query.disk_usage(conn, str(weird), depth=1)
    assert sum(r["bytes"] for r in rows) == 42


# --- forget ----------------------------------------------------------------

def test_forget_removes_rows_and_the_root(conn, tree):
    scan_root(conn, HOST, tree)
    before = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert before > 0

    removed, known = query.forget_root(conn, str(tree), HOST)
    assert known is True
    assert removed == before
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM roots").fetchone()[0] == 0


def test_forget_leaves_other_roots_alone(conn, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "one.txt").write_text("1")
    (b / "two.txt").write_text("2")
    scan_root(conn, HOST, a)
    scan_root(conn, HOST, b)

    query.forget_root(conn, str(a), HOST)
    remaining = {r["name"] for r in conn.execute("SELECT name FROM files")}
    assert remaining == {"two.txt"}


def test_forget_an_unknown_root_reports_it(conn, tmp_path):
    removed, known = query.forget_root(conn, str(tmp_path / "never-scanned"), HOST)
    assert (removed, known) == (0, False)


def test_forget_does_not_touch_the_filesystem(conn, tree):
    scan_root(conn, HOST, tree)
    query.forget_root(conn, str(tree), HOST)
    assert (tree / "loose.txt").exists()


# --- progress --------------------------------------------------------------

def test_progress_is_called_on_a_big_enough_tree(conn, tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    for i in range(PROGRESS_EVERY + 10):
        (root / f"f{i}").write_text("x")

    seen = []
    scan_root(conn, HOST, root, progress=lambda stats: seen.append(stats.total))
    assert seen, "progress callback was never invoked"
    assert all(n % PROGRESS_EVERY == 0 for n in seen)


def test_scan_without_a_progress_callback_still_works(conn, tree):
    stats = scan_root(conn, HOST, tree, progress=None)
    assert stats.files == 4


# --- CLI wiring ------------------------------------------------------------

def test_cli_du_and_forget(tmp_path, monkeypatch, capsys):
    from cdm import cli
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", "testhost")

    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.bin").write_bytes(b"x" * 2048)

    assert cli.main(["scan", str(root)]) == 0
    capsys.readouterr()

    assert cli.main(["du", str(root)]) == 0
    assert "sub" in capsys.readouterr().out

    assert cli.main(["forget", str(root)]) == 0
    assert "row(s) removed" in capsys.readouterr().err

    assert cli.main(["du", str(root)]) == 1
    assert "Scan it first" in capsys.readouterr().err


def test_cli_forget_unknown_root_exits_1(tmp_path, monkeypatch, capsys):
    from cdm import cli
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", "testhost")
    assert cli.main(["forget", "/not/indexed"]) == 1
    assert "not a known root" in capsys.readouterr().err


def test_progress_is_silent_when_stderr_is_not_a_tty(tmp_path, monkeypatch, capsys):
    """Redirected output must not fill up with \\r counters."""
    from cdm import cli
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f").write_text("x")

    cli.main(["scan", str(root)])
    assert "scanned" not in capsys.readouterr().err
    assert paths.index_path().exists()
