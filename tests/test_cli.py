"""End-to-end through the CLI, including the file modes the index promises."""
from __future__ import annotations

import os
import stat

import pytest

from cdm import cli, hashing, paths


@pytest.fixture(autouse=True)
def isolated_index(tmp_path, monkeypatch):
    """Never touch the real index while testing."""
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", "testhost")
    yield


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "big.bin").write_bytes(b"x" * 5000)
    return root


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0


def test_no_args_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_scan_then_find(tree, capsys):
    assert cli.main(["scan", str(tree)]) == 0
    capsys.readouterr()

    assert cli.main(["find", "--larger-than", "1k", "--quiet"]) == 0
    out = capsys.readouterr().out
    assert "big.bin" in out
    assert "a.txt" not in out


def test_index_is_created_0600(tree):
    cli.main(["scan", str(tree)])
    mode = stat.S_IMODE(os.stat(paths.index_path()).st_mode)
    assert mode == 0o600, f"index is {oct(mode)}, expected 0600"


def test_data_dir_is_0700(tree):
    cli.main(["scan", str(tree)])
    mode = stat.S_IMODE(os.stat(paths.data_dir()).st_mode)
    assert mode == 0o700, f"data dir is {oct(mode)}, expected 0700"


def test_scan_of_a_missing_path_is_an_error(tmp_path, capsys):
    assert cli.main(["scan", str(tmp_path / "nope")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_quiet_find_prints_only_paths(tree, capsys):
    cli.main(["scan", str(tree)])
    capsys.readouterr()
    cli.main(["find", "--quiet", "--type", "file"])
    out = capsys.readouterr().out.strip().splitlines()
    assert all(line.startswith("/") for line in out)


def test_find_json_is_parseable(tree, capsys):
    import json
    cli.main(["scan", str(tree)])
    capsys.readouterr()
    cli.main(["find", "--json", "--type", "file"])
    rows = json.loads(capsys.readouterr().out)
    assert {r["name"] for r in rows} == {"a.txt", "big.bin"}


def test_bad_size_is_a_usage_error_not_a_traceback(tree, capsys):
    cli.main(["scan", str(tree)])
    capsys.readouterr()
    assert cli.main(["find", "--larger-than", "enormous"]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_roots_lists_what_was_scanned(tree, capsys):
    cli.main(["scan", str(tree)])
    capsys.readouterr()
    cli.main(["roots"])
    assert str(tree) in capsys.readouterr().out


def test_rescan_without_roots_exits_3(capsys):
    assert cli.main(["rescan"]) == 3
    assert "no roots" in capsys.readouterr().err


def test_rescan_all_known_roots(tree, capsys):
    cli.main(["scan", str(tree)])
    capsys.readouterr()
    assert cli.main(["rescan"]) == 0
    assert str(tree) in capsys.readouterr().err


def test_stat_reports_a_known_file(tree, capsys):
    cli.main(["scan", str(tree), "--checksum"])
    capsys.readouterr()
    assert cli.main(["stat", str(tree / "a.txt")]) == 0
    out = capsys.readouterr().out
    assert "hash" in out and hashing.PARTIAL in out


def test_stat_of_an_unknown_file_exits_1(tree, capsys):
    cli.main(["scan", str(tree)])
    capsys.readouterr()
    assert cli.main(["stat", "/nonexistent/path"]) == 1
    assert "not in the index" in capsys.readouterr().err


def test_doctor_without_an_index_exits_1(capsys):
    assert cli.main(["doctor"]) == 1
    assert "no index" in capsys.readouterr().err


def test_doctor_reports_a_healthy_index(tree, capsys):
    cli.main(["scan", str(tree)])
    capsys.readouterr()
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "0o600" in out
    assert "entries" in out


def test_dupes_needs_checksums(tree, capsys):
    cli.main(["scan", str(tree)])
    capsys.readouterr()
    cli.main(["dupes"])
    assert "--checksum" in capsys.readouterr().err


def test_dupes_reports_a_pair(tmp_path, capsys):
    root = tmp_path / "dupes"
    root.mkdir()
    (root / "one.txt").write_text("identical content")
    (root / "two.txt").write_text("identical content")
    cli.main(["scan", str(root), "--checksum"])
    capsys.readouterr()

    assert cli.main(["dupes"]) == 0
    captured = capsys.readouterr()
    assert "one.txt" in captured.out and "two.txt" in captured.out
    assert "reclaimable" in captured.err
