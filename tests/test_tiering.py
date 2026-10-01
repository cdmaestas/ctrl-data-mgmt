"""The pool-aware suggestion: cold data on the fast pool, with a draft rule.

The drafted rule's syntax and selection were checked against a real Storage
Scale policy engine (`mmapplypolicy -I test`); these tests pin what cdm drafts.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest
from test_policy import HEADER, line

from cdm import cli, db, importer, policy, roots, suggest

DAY = 86400
NOW = time.time()
OLD = datetime.fromtimestamp(NOW - 400 * DAY, tz=timezone.utc).strftime(
    "%Y-%m-%d %H:%M:%S.%f")
RECENT = datetime.fromtimestamp(NOW - 2 * DAY, tz=timezone.utc).strftime(
    "%Y-%m-%d %H:%M:%S.%f")
P = "/gpfs/fs1/proj"


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "index.db")
    yield c
    c.close()


def load(conn, tmp_path, *rows, scope="fileset proj", setting="relatime", root=P):
    head = [h.replace("# scope: fileset proj", f"# scope: {scope}")
             .replace("# suppress_atime: relatime", f"# suppress_atime: {setting}")
            for h in HEADER]
    p = tmp_path / "l.raw"
    p.write_text("".join(head + list(rows) + [f"{policy.END}{len(rows)}\n"]))
    importer.import_policy(conn, p, root=root)
    return roots.visible_hosts(conn, "laptop")


def f(path, *, pool="system", age=OLD, size=1000, fileset="proj"):
    return line(path, size=size, mtime=age, atime=age, pool=pool, fileset=fileset)


def tier(conn, hosts, **kw):
    found = suggest.suggest(conn, host=hosts, now=NOW, **kw)["suggestions"]
    return [s for s in found if s["id"].startswith("tier:")]


def test_cold_data_on_the_fast_pool_is_suggested_with_a_scoped_rule(conn, tmp_path):
    hosts = load(conn, tmp_path,
                 f(f"{P}/cold/a", size=3000), f(f"{P}/cold/b", size=2000),
                 f(f"{P}/warm/c", age=RECENT), f(f"{P}/tiered/d", pool="data1"))
    [s] = tier(conn, hosts)
    assert s["bytes"] == 5000 and s["risk"] == suggest.REVIEW
    assert "MIGRATE FROM POOL 'system' TO POOL 'data1'" in s["draft"]
    assert "FOR FILESET('proj')" in s["draft"]
    assert "DAYS(CURRENT_TIMESTAMP) - DAYS(ACCESS_TIME) > 180" in s["draft"]
    assert "DAYS(CURRENT_TIMESTAMP) - DAYS(MODIFICATION_TIME) > 180" in s["draft"]
    assert "-I test" in s["draft"] and "-I test" in s["action"]
    assert [i["path"] for i in s["items"]] == [f"{P}/cold"]


def test_a_whole_filesystem_listing_drafts_a_whole_filesystem_rule(conn, tmp_path):
    hosts = load(conn, tmp_path, f(f"{P}/a"), f(f"{P}/b", pool="data1"),
                 scope="filesystem", root="/gpfs/fs1")
    [s] = tier(conn, hosts)
    assert "FOR FILESET" not in s["draft"]


def test_unknown_access_times_suggest_nothing(conn, tmp_path):
    hosts = load(conn, tmp_path, f(f"{P}/a"), f(f"{P}/b", pool="data1"), setting="yes")
    assert tier(conn, hosts) == []


def test_nothing_without_somewhere_to_move_it_unless_told(conn, tmp_path):
    hosts = load(conn, tmp_path, f(f"{P}/a"), f(f"{P}/b"))
    assert tier(conn, hosts) == []
    [s] = tier(conn, hosts, cold_pool="capacity")
    assert "TO POOL 'capacity'" in s["draft"]


def test_several_slower_pools_leave_the_choice_to_the_admin(conn, tmp_path):
    hosts = load(conn, tmp_path, f(f"{P}/a"), f(f"{P}/b", pool="data1"),
                 f(f"{P}/c", pool="data2"))
    [s] = tier(conn, hosts)
    assert "TO POOL '<slower pool>'" in s["draft"]
    assert "data1, data2" in s["detail"]


def test_the_threshold_and_fast_pool_are_configurable(conn, tmp_path):
    hosts = load(conn, tmp_path, f(f"{P}/a", age=RECENT), f(f"{P}/b", pool="data1"))
    assert tier(conn, hosts) == []
    [s] = tier(conn, hosts, cold_days=1)
    assert "> 1\n" in s["draft"]
    [s] = tier(conn, hosts, fast_pool="data1", cold_pool="system", cold_days=1)
    assert "FROM POOL 'data1' TO POOL 'system'" in s["draft"]


def test_without_names_the_rule_has_no_fileset_name(conn, tmp_path):
    hosts = load(conn, tmp_path, f(f"{P}/a", fileset="jsmith-merger"),
                 f(f"{P}/b", pool="data1", fileset="jsmith-merger"),
                 scope="fileset jsmith-merger")
    hidden = suggest.suggest(conn, host=hosts, now=NOW, names=False)["suggestions"]
    [s] = [x for x in hidden if x["id"].startswith("tier:")]
    assert "FOR FILESET('<fileset>')" in s["draft"]
    assert "jsmith" not in json.dumps(s) and "items" not in s


def test_an_odd_name_never_goes_into_a_rule(conn, tmp_path):
    hosts = load(conn, tmp_path, f(f"{P}/a", pool="fast'pool"),
                 f(f"{P}/b", pool="data1"))
    assert tier(conn, hosts, fast_pool="fast'pool") == []


def test_the_listing_scope_is_recorded_on_the_root(conn, tmp_path):
    load(conn, tmp_path, f(f"{P}/a"))
    assert conn.execute("SELECT scope FROM roots").fetchone()[0] == "fileset proj"


def test_cdm_suggest_prints_the_draft(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    head = list(HEADER)
    p = tmp_path / "l.raw"
    rows = [f(f"{P}/a"), f(f"{P}/b", pool="data1")]
    p.write_text("".join(head + rows + [f"{policy.END}{len(rows)}\n"]))
    assert cli.main(["import", "--policy", str(p), "--root", P]) == 0
    capsys.readouterr()
    assert cli.main(["suggest", "--cold-after", "30"]) == 0
    out = capsys.readouterr().out
    assert "Cold data on the system pool" in out
    assert "RULE 'cdm-cold-to-data1' MIGRATE FROM POOL 'system'" in out
