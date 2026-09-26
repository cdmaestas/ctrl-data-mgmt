"""Disk-usage rollup, root removal, and scan progress."""
from __future__ import annotations

import pytest

from cdm import db, paths, query
from cdm import scan as scan_mod
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

    out = query.forget_root(conn, str(tree), HOST)
    assert out.known is True
    assert out.removed == before
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
    out = query.forget_root(conn, str(tmp_path / "never-scanned"), HOST)
    assert (out.removed, out.known) == (0, False)


def test_forget_does_not_touch_the_filesystem(conn, tree):
    scan_root(conn, HOST, tree)
    query.forget_root(conn, str(tree), HOST)
    assert (tree / "loose.txt").exists()


# --- nested roots ----------------------------------------------------------

@pytest.fixture()
def nested(conn, tree):
    """`tree` and `tree/big` both registered, scanned outer first then inner."""
    scan_root(conn, HOST, tree, hash_kind="partial")
    scan_root(conn, HOST, tree / "big", hash_kind="partial")
    return tree, tree / "big"


def owners(conn):
    return {r["name"]: r["root"] for r in conn.execute("SELECT name, root FROM files")}


def test_forget_the_outer_root_keeps_the_nested_roots_rows(conn, nested):
    outer, inner = nested
    out = query.forget_root(conn, str(outer), HOST)
    assert out.nested == [str(inner)]
    # big/ itself, small/, small/c.txt, loose.txt -- not what is inside big/.
    assert out.removed == 4
    left = owners(conn)
    assert left == {"a.bin": str(inner), "deep": str(inner), "b.bin": str(inner)}
    assert [r[0] for r in conn.execute("SELECT path FROM roots")] == [str(inner)]


def test_forget_the_nested_root_hands_its_rows_to_the_outer_one(conn, nested):
    """The outer root still covers them; deleting would only force a re-hash."""
    outer, inner = nested
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    out = query.forget_root(conn, str(inner), HOST)
    assert (out.removed, out.handed_over, out.handed_to) == (0, 3, str(outer))
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == total
    assert set(owners(conn).values()) == {str(outer)}

    stats = scan_root(conn, HOST, outer, hash_kind="partial")
    assert stats.hashed == 0


def test_du_and_dupes_count_each_file_once_with_nested_roots(conn, nested):
    outer, _ = nested
    (outer / "big" / "copy.bin").write_bytes(b"x" * 3000)   # same as a.bin
    scan_root(conn, HOST, outer, hash_kind="partial")
    scan_root(conn, HOST, outer / "big", hash_kind="partial")

    rows = query.disk_usage(conn, str(outer), depth=1)
    assert sum(r["bytes"] for r in rows) == 3000 + 3000 + 5000 + 100 + 50
    groups = query.dupe_groups(conn, host=HOST)
    assert [len(g["members"]) for g in groups] == [2]


def test_find_root_includes_nested_roots(conn, nested):
    outer, inner = nested
    names = {r["name"] for r in query.find(conn, host=HOST, root=str(outer), kind="file")}
    assert names == {"a.bin", "b.bin", "c.txt", "loose.txt"}
    names = {r["name"] for r in query.find(conn, host=HOST, root=str(inner), kind="file")}
    assert names == {"a.bin", "b.bin"}


def test_summary_counts_each_file_once_and_marks_nesting(conn, nested):
    from cdm import shape
    outer, inner = nested
    out = shape.summary(conn, host=HOST, root=str(outer))
    by_root = {r["root"]: r for r in out["roots"]}
    assert by_root[str(outer)]["files"] == 2 and by_root[str(inner)]["files"] == 2
    assert by_root[str(inner)]["inside"] == str(outer)
    assert by_root[str(outer)]["inside"] is None
    assert out["total_files"] == 4
    assert shape.size_histogram(conn, host=HOST, root=str(outer))["total_files"] == 4


def test_cli_roots_and_forget_explain_nesting(tmp_path, monkeypatch, capsys):
    from cdm import cli
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    outer = tmp_path / "home"
    (outer / "src").mkdir(parents=True)
    (outer / "src" / "f").write_text("x")
    (outer / "g").write_text("y")
    outer, inner = outer.resolve(), (outer / "src").resolve()

    assert cli.main(["scan", str(outer)]) == 0
    capsys.readouterr()
    assert cli.main(["scan", str(inner)]) == 0
    assert f"inside root {outer}" in capsys.readouterr().err
    # A rescan of a known root says nothing more about it.
    assert cli.main(["scan", str(inner)]) == 0
    assert "inside root" not in capsys.readouterr().err

    assert cli.main(["roots"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith(f"{inner} ") and f"(inside {outer})" in line
               for line in lines)

    assert cli.main(["forget", str(inner)]) == 0
    assert f"now belong to {outer}" in capsys.readouterr().err


# --- progress --------------------------------------------------------------

def test_progress_is_called_on_a_big_enough_tree(conn, tmp_path, monkeypatch):
    # Count-triggered only; the time trigger has its own test below.
    monkeypatch.setattr(scan_mod, "PROGRESS_SECONDS", float("inf"))
    root = tmp_path / "many"
    root.mkdir()
    for i in range(PROGRESS_EVERY + 10):
        (root / f"f{i}").write_text("x")

    seen = []
    scan_root(conn, HOST, root, progress=lambda stats: seen.append(stats.total))
    assert seen, "progress callback was never invoked"
    # Counts advance and are not spammed. Exact multiples of PROGRESS_EVERY are
    # not the contract: entries are recorded a directory at a time, so a single
    # batch can cross the threshold by any amount.
    assert seen == sorted(seen)
    assert all(b - a >= PROGRESS_EVERY for a, b in zip(seen, seen[1:]))


def test_scan_without_a_progress_callback_still_works(conn, tree):
    stats = scan_root(conn, HOST, tree, progress=None)
    assert stats.files == 4


def test_progress_is_also_time_triggered(conn, tree, monkeypatch):
    """A slow filesystem that never reaches PROGRESS_EVERY still reports."""
    monkeypatch.setattr(scan_mod, "PROGRESS_SECONDS", 0.0)
    seen = []
    scan_root(conn, HOST, tree, progress=lambda stats: seen.append(stats.running_for))
    assert seen, "time-triggered progress never fired on a small tree"
    assert all(t > 0 for t in seen)


def test_rates_are_per_second_and_final_after_the_scan(conn, tree):
    stats = scan_root(conn, HOST, tree, hash_kind="partial")
    per_sec, bytes_per_sec = stats.rates()
    assert stats.running_for == stats.elapsed > 0
    assert per_sec == pytest.approx(stats.total / stats.elapsed)
    assert stats.hashed_bytes == 3000 + 5000 + 100 + 50
    assert bytes_per_sec == pytest.approx(stats.hashed_bytes / stats.elapsed)


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


def test_progress_flag_logs_rate_lines_without_a_tty(tmp_path, monkeypatch, capsys):
    """--progress is how a background scan or a log gets a rate."""
    from cdm import cli
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(scan_mod, "PROGRESS_SECONDS", 0.0)
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f").write_text("x")

    assert cli.main(["scan", str(root), "--checksum", "--progress", "0"]) == 0
    err = capsys.readouterr().err
    progress = [line for line in err.splitlines() if "scanned" in line]
    assert progress, err
    assert "/s)" in progress[0] and "\r" not in err
    # The summary carries the rate too, so it is there without --progress.
    assert "entries/s)" in err
    assert "read, " in err
