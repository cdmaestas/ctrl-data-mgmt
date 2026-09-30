"""The fixture sanitizer, and the committed fixture it produced.

The leak check that matters runs inside the sanitizer, on the cluster, where
the raw input is. Here the sanitizer is tested on synthetic raw listings with
telltale names, and the committed fixture is checked structurally: every name
in it must be one the sanitizer writes. Raw-looking lines are assembled at run
time so this file never trips scripts/check_raw_data.py.
"""
from __future__ import annotations

import importlib.util
import re
import stat
from pathlib import Path
from urllib.parse import quote

import pytest
from test_policy import HEADER, show

from cdm import policy

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "storage-scale-fileset.list"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


san = _load("sanitize_listing")
guard = _load("check_raw_data")

SECRETS = ("secretfs", "acme", "jsmith", "bluebird", "merger", "fastssd", "5023")


def raw_listing(*extra: str, end: bool = True) -> list[str]:
    head = [h.replace("fs1", "secretfs").replace("cluster1.example", "acme.hq")
             .replace("fileset proj", "fileset jsmith") for h in HEADER]

    def entry(path, **kw):
        kw.setdefault("fileset", "jsmith")
        return f"544769 1 0  {show(**kw)} -- {quote(path.encode(), safe='/')}\n"

    base = "/gpfs/secretfs/jsmith"
    rows = [entry(base, mode="drwxr-xr-x"),
            entry(f"{base}/Project Bluebird", mode="drwxr-xr-x"),
            entry(f"{base}/Project Bluebird/merger-memo.pdf", uid=5023, gid=77),
            entry(f"{base}/Project Bluebird/merger-memo.pdf.bak", uid=5023),
            entry(f"{base}/Other/merger-memo.pdf"),
            entry(f"{base}/.ssh", mode="drwx------"),
            entry(f"{base}/café notes.txt", pool="fastssd"),
            entry(f"{base}/system", mode="drwxr-xr-x"),
            entry(f"{base}/root-owned", uid=0, gid=0),
            entry(f"{base}/latest", mode="lrwxrwxrwx"), *extra]
    return head + rows + ([f"{policy.END}{len(rows)}\n"] if end else [])


def sanitized(lines=None):
    out, secrets, n = san.sanitize(iter(lines or raw_listing()))
    return "".join(out), secrets, n


def entries(text):
    header, it, state = policy.read_listing(text.splitlines(keepends=True))
    rows = list(it)
    assert state["complete"]
    return header, rows


# --- what the sanitizer does --------------------------------------------------

def test_no_real_name_survives():
    text, secrets, _ = sanitized()
    for word in SECRETS:
        assert word not in text.lower(), word
    assert not san.leaks(text, secrets)


def test_the_output_is_a_complete_listing_marked_sanitized():
    text, _, n = sanitized()
    assert text.startswith(policy.SANITIZED) and text.startswith(guard.HEADER)
    header, rows = entries(text)
    assert len(rows) == n == 10
    assert (header["device"], header["cluster"], header["scope"]) == \
        ("fs1", "cluster1.example", "fileset fileset1")
    assert header["sanitized"] == san.VERSION
    assert not guard.problems("tests/fixtures/x.list", text.encode())


def test_names_become_consistent_placeholders_that_keep_shape():
    _, rows = entries(sanitized()[0])
    paths = [r.path for r in rows]
    assert paths[0] == "/gpfs/fs1/fileset1"
    assert re.fullmatch(r"/gpfs/fs1/fileset1/d\d{4}/f\d{4}\.pdf", paths[2])
    # The same name is the same placeholder wherever it appears...
    assert paths[2].rsplit("/", 1)[1] == paths[4].rsplit("/", 1)[1]
    # ...an unknown extension is dropped, a hidden name keeps its dot...
    assert re.fullmatch(r"f\d{4}", paths[3].rsplit("/", 1)[1])
    assert re.fullmatch(r"\.d\d{4}", paths[5].rsplit("/", 1)[1])
    # ...and kinds keep their letter.
    assert re.fullmatch(r"l\d{4}", paths[9].rsplit("/", 1)[1])


def test_ids_pools_and_filesets_are_renumbered():
    _, rows = entries(sanitized()[0])
    by_name = {r.path: r for r in rows}
    assert {r.uid for r in rows} == {0, 1000, 1001}
    assert rows[8].uid == 0, "root stays root"
    assert {r.pool for r in rows} == {"system", "data1"}
    assert {r.fileset for r in rows} == {"fileset1"}
    assert len(by_name) == len(rows)


def test_numbers_and_times_are_kept():
    raw = raw_listing()
    _, raw_rows = entries("".join(raw))
    _, rows = entries(sanitized(raw)[0])
    for a, b in zip(raw_rows, rows):
        assert (a.inode, a.size, a.mtime, a.atime, a.ctime, a.mode, a.nlink) == \
               (b.inode, b.size, b.mtime, b.atime, b.ctime, b.mode, b.nlink)


def test_a_directory_named_like_the_output_vocabulary_is_no_false_alarm():
    """A real directory called `system` is replaced; the word itself is ours."""
    text, secrets, _ = sanitized()
    assert "system" in secrets and not san.leaks(text, secrets)


def test_a_leak_is_caught(monkeypatch):
    real = san.Sanitizer.component
    monkeypatch.setattr(san.Sanitizer, "component",
                        lambda self, name, kind: name if name == "Project Bluebird"
                        else real(self, name, kind))
    text, secrets, _ = sanitized()
    assert san.leaks(text, secrets) == ["Project Bluebird"]


def test_an_incomplete_listing_is_refused():
    with pytest.raises(policy.ListingError, match="end marker"):
        sanitized(raw_listing(end=False))


# --- the command --------------------------------------------------------------

def test_the_command_writes_a_private_file(tmp_path):
    src = tmp_path / "in.list.raw"
    src.write_text("".join(raw_listing()))
    dst = tmp_path / "out.list"
    assert san.main([str(src), str(dst)]) == 0
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
    assert "bluebird" not in dst.read_text().lower()


def test_the_command_refuses_raw_output_and_overwriting_its_input(tmp_path):
    src = tmp_path / "in.list.raw"
    src.write_text("".join(raw_listing()))
    assert san.main([str(src), str(tmp_path / "out.list.raw")]) == 2
    same = tmp_path / "in.list"
    same.write_text("".join(raw_listing()))
    assert san.main([str(same), str(same)]) == 2


def test_a_leak_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(san, "leaks", lambda text, secrets: ["x"])
    src = tmp_path / "in.list.raw"
    src.write_text("".join(raw_listing()))
    assert san.main([str(src), str(tmp_path / "out.list")]) == 1
    assert not (tmp_path / "out.list").exists()
    assert "nothing was written" in capsys.readouterr().err


def test_the_guard_and_the_reader_agree_on_the_marker():
    assert guard.HEADER == policy.SANITIZED


# --- the committed fixture ----------------------------------------------------

_PLACEHOLDER = re.compile(r"^\.?[dfl]\d{4}(\.[a-z0-9]+)?$")


def fixture():
    return entries(FIXTURE.read_text())


def test_the_fixture_is_complete_generic_and_accepted_by_the_guard():
    header, rows = fixture()
    assert header["sanitized"] == san.VERSION
    assert (header["device"], header["cluster"]) == ("fs1", "cluster1.example")
    assert re.fullmatch(r"fileset fileset\d+|filesystem", header["scope"])
    assert not guard.problems(str(FIXTURE.relative_to(ROOT)), FIXTURE.read_bytes())
    assert rows


def test_every_name_in_the_fixture_is_one_the_sanitizer_writes():
    """The structural leak test: nothing but placeholders and our own words."""
    _, rows = fixture()
    for r in rows:
        for part in r.path.strip("/").split("/"):
            assert part in {"gpfs", "fs1"} or re.fullmatch(r"fileset\d+", part) \
                or _PLACEHOLDER.match(part), f"unexpected name {part!r} in {r.path}"
            ext = part.rpartition(".")[2] if _PLACEHOLDER.match(part) and "." in \
                part.lstrip(".") else None
            assert ext is None or ext in san.KEEP_EXTENSIONS
        assert re.fullmatch(r"fileset\d+", r.fileset)
        assert r.pool == "system" or re.fullmatch(r"data\d+", r.pool)
        assert r.uid == 0 or r.uid >= 1000
        assert r.gid == 0 or r.gid >= 1000


def test_the_fixture_has_what_phase_2_tests_need():
    """Directories, links, hard links, two pools, and a never-opened file."""
    _, rows = fixture()
    kinds = {r.kind for r in rows}
    assert kinds == {"dir", "file", "link"}
    assert {r.pool for r in rows} >= {"system", "data1"}
    assert any(r.nlink == 2 and r.kind == "file" for r in rows)
    assert any(r.kind == "file" and r.atime < r.mtime for r in rows)
    assert any(r.path.rsplit("/", 1)[1].startswith(".") for r in rows)


def test_a_numeric_directory_name_is_no_false_alarm_in_the_numbers():
    """`2026` is replaced in the path; the same digits in a timestamp are not a leak."""
    raw = raw_listing()
    extra = raw[-1:]
    row = raw[len(HEADER)].replace("/gpfs/secretfs/jsmith", "/gpfs/secretfs/jsmith/2026")
    lines = raw[:-1] + [row] + [f"{policy.END}{int(extra[0].split('=')[1]) + 1}\n"]
    text, names, _ = san.sanitize(iter(lines))
    assert "2026" in names and "2026" in "".join(text)       # it is in the times
    assert not san.leaks("".join(text), names)
