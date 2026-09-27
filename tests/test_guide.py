"""The guide: every status is read from the index, and it always says what is next."""
from __future__ import annotations

import json
import plistlib
import time

import pytest

from cdm import cli, db, guide, hashing, tools
from cdm.scan import scan_root

HOST = "testhost"
DAY = 86400


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "index.db")
    yield c
    c.close()


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "work"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("alpha")
    (root / "sub" / "b.txt").write_text("beta")
    return root


def statuses(result):
    return {s["id"]: s["status"] for s in result["steps"]}


def test_an_empty_index_starts_at_step_one(conn):
    result = guide.guide(conn, host=HOST)
    assert result["next"] == "index"
    assert result["done"] == 0
    st = statuses(result)
    assert st["index"] == st["hash"] == st["fresh"] == guide.TODO
    assert st["ai"] == guide.OPTIONAL


def test_a_stat_only_scan_moves_on_to_hashing(conn, tree):
    scan_root(conn, HOST, tree)
    result = guide.guide(conn, host=HOST)
    assert statuses(result)["index"] == guide.DONE
    assert result["next"] == "hash"
    hash_step = next(s for s in result["steps"] if s["id"] == "hash")
    assert hash_step["command"] == f"cdm rescan --checksum {tree.resolve()}"


def test_a_hashed_fresh_index_has_every_checkable_step_done(conn, tree):
    scan_root(conn, HOST, tree, hash_kind=hashing.PARTIAL)
    result = guide.guide(conn, host=HOST)
    st = statuses(result)
    assert st["index"] == st["hash"] == st["fresh"] == guide.DONE
    assert result["done"] == result["of"]
    # Nothing is left to check, so it points at something useful, never nowhere.
    assert result["next"] in {s["id"] for s in result["steps"]}


def test_steps_the_index_cannot_observe_never_claim_to_be_done(conn, tree):
    scan_root(conn, HOST, tree, hash_kind=hashing.PARTIAL)
    st = statuses(guide.guide(conn, host=HOST))
    assert st["act"] == guide.ADVICE
    assert st["ai"] == guide.OPTIONAL


def test_an_old_scan_reopens_the_keep_current_step(conn, tree):
    scan_root(conn, HOST, tree, hash_kind=hashing.PARTIAL)
    result = guide.guide(conn, host=HOST, now=time.time() + 30 * DAY)
    fresh = next(s for s in result["steps"] if s["id"] == "fresh")
    assert fresh["status"] == guide.TODO
    assert result["next"] == "fresh"
    assert "30 days" in fresh["detail"]


def test_something_to_clean_up_is_advice_and_nothing_is_done(conn, tmp_path, monkeypatch):
    from cdm import suggest
    monkeypatch.setattr(suggest, "MIN_KNOWN_CACHE", 1)
    root = tmp_path / "home"
    (root / ".npm" / "_cacache").mkdir(parents=True)
    (root / ".npm" / "_cacache" / "x").write_bytes(b"x" * 100)
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    step = next(s for s in guide.guide(conn, host=HOST)["steps"] if s["id"] == "suggest")
    assert step["status"] == guide.ADVICE
    assert step["detail"].startswith("1 safe")

    (root / ".npm" / "_cacache" / "x").unlink()
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    step = next(s for s in guide.guide(conn, host=HOST)["steps"] if s["id"] == "suggest")
    assert step["status"] == guide.DONE


def test_no_path_below_a_root_appears(conn, tmp_path):
    root = tmp_path / "home"
    (root / "codename-bluebird").mkdir(parents=True)
    (root / "codename-bluebird" / "quokka.txt").write_text("x")
    scan_root(conn, HOST, root)
    assert "bluebird" not in json.dumps(guide.guide(conn, host=HOST))
    assert "quokka" not in json.dumps(guide.guide(conn, host=HOST))


# --- scheduling ----------------------------------------------------------------

def test_the_launchd_job_is_a_valid_plist_on_its_own():
    job, how = guide.schedule("darwin", executable="/opt/cdm", hour=2, minute=5)
    plist = plistlib.loads(job.encode())
    assert plist["ProgramArguments"][:3] == ["/opt/cdm", "rescan", "--checksum"]
    assert plist["StartCalendarInterval"] == {"Hour": 2, "Minute": 5}
    assert "launchctl bootstrap" in how and "bootout" in how


def test_the_cron_job_is_one_line_with_an_absolute_path():
    job, how = guide.schedule("linux", executable="/opt/cdm", hour=2, minute=5)
    assert job.count("\n") == 1
    assert job.startswith("5 2 * * * ") and "/opt/cdm rescan --checksum" in job
    assert "crontab" in how


# --- front-ends -----------------------------------------------------------------

def test_cli_guide_marks_the_next_step_and_explains_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", HOST)
    assert cli.main(["guide"]) == 0
    out = capsys.readouterr().out
    assert "0 of 3 done" in out
    assert "[>] 1  Index a folder" in out
    assert "$ cdm scan ~/work --checksum" in out
    # Only the next step is explained unless --all.
    assert out.count("$ ") == 1

    assert cli.main(["guide", "--all"]) == 0
    assert capsys.readouterr().out.count("$ ") == 6


def test_cli_schedule_sends_only_the_job_to_stdout(capsys):
    assert cli.main(["guide", "--schedule"]) == 0
    out, err = capsys.readouterr()
    assert "rescan" in out and "--checksum" in out
    assert "To install" in err and "To install" not in out


def test_catalog_guide_and_the_getting_started_prompt(tmp_path, tree):
    index = tmp_path / "index.db"
    c = db.connect(index)
    scan_root(c, HOST, tree)
    c.close()
    out = tools.Catalog(index=index, host=HOST).guide()
    assert out["next"] == "hash" and out["next_steps"]
    text = tools.prompt_text("getting-started", expose_names=False)
    assert "`guide`" in text and "one step at a time" in text


def test_progress_has_a_fixed_total(conn, tree):
    empty = guide.guide(conn, host=HOST)
    scan_root(conn, HOST, tree, hash_kind=hashing.PARTIAL)
    full = guide.guide(conn, host=HOST)
    assert (empty["done"], empty["of"]) == (0, 3)
    assert (full["done"], full["of"]) == (3, 3)


# --- hardening ------------------------------------------------------------------

HOSTILE = "/Users/o'neil & <co>/100%/bin/cdm"


def test_the_launchd_job_survives_a_hostile_path():
    """A template put the path into XML verbatim: `&` or `<` broke the plist."""
    job, _ = guide.schedule("darwin", executable=HOSTILE)
    assert plistlib.loads(job.encode())["ProgramArguments"][0] == HOSTILE


def test_the_cron_line_passes_a_hostile_path_as_one_word():
    """Unquoted, `&` or `;` ran as more than one command; cron turns `%` into newline."""
    import shlex
    job, _ = guide.schedule("linux", executable=HOSTILE)
    command = job.split(" ", 5)[5]
    assert "\\%" in command and "%/" not in command.replace("\\%", "")
    assert shlex.split(command.replace("\\%", "%"))[3] == HOSTILE


def test_without_cdm_on_path_the_job_runs_this_python(monkeypatch):
    """Previously sys.argv[0]: under `python -m cdm`, a path to __main__.py."""
    import sys
    monkeypatch.setattr(guide.shutil, "which", lambda name: None)
    job, _ = guide.schedule("darwin")
    assert plistlib.loads(job.encode())["ProgramArguments"][:3] == [sys.executable, "-m", "cdm"]
