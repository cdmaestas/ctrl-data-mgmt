"""Storage Scale pools and filesets: filters, totals, and whose names show.

Pool names are shape; fileset names are names (docs/adr/0007): the default MCP
server must report filesets by rank only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_policy import HEADER, line

from cdm import cli, db, hashing, importer, policy, query, roots, shape, tools
from cdm.scan import scan_root

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "storage-scale-fileset.list"
SECRET = "jsmith-merger"          # a fileset named like a person's project


def listing(*rows, scope="filesystem"):
    head = [h.replace("# scope: fileset proj", f"# scope: {scope}") for h in HEADER]
    return head + list(rows) + [f"{policy.END}{len(rows)}\n"]


@pytest.fixture()
def index(tmp_path):
    """Two filesets on two pools, plus a local scan with neither."""
    path = tmp_path / "index.db"
    c = db.connect(path)
    src = tmp_path / "x.list.raw"
    src.write_text("".join(listing(
        line("/gpfs/fs1/a/big", size=3000, fileset="projects", pool="system"),
        line("/gpfs/fs1/a/cold", size=9000, fileset="projects", pool="data1"),
        line(f"/gpfs/fs1/{SECRET}/x", size=100, fileset=SECRET, pool="system"))))
    importer.import_policy(c, src, root="/gpfs/fs1")
    local = tmp_path / "home"
    local.mkdir()
    (local / "f").write_bytes(b"x" * 50)
    scan_root(c, "laptop", local, hash_kind=hashing.PARTIAL)
    yield c, path
    c.close()


def hosts(c):
    return roots.visible_hosts(c, "laptop")


def test_find_filters_by_pool_and_fileset(index):
    c, _ = index
    assert [r["name"] for r in query.find(c, host=hosts(c), pool="data1")] == ["cold"]
    assert {r["name"] for r in query.find(c, host=hosts(c), fileset="projects")} == \
        {"big", "cold"}
    assert query.find(c, host=hosts(c), pool="nonesuch") == []


def test_totals_by_pool_name_and_by_fileset_rank(index):
    c, _ = index
    out = shape.storage(c, host=hosts(c))
    assert [(p["pool"], p["bytes"]) for p in out["pools"]] == [("data1", 9000),
                                                               ("system", 3100)]
    assert [(f["fileset"], f["bytes"]) for f in out["filesets"]] == [
        ("fileset #1", 12000), ("fileset #2", 100)]
    assert out["not_from_a_listing"]["files"] == 1, "the scanned file has neither"
    assert SECRET not in json.dumps(out)


def test_fileset_names_appear_only_when_asked_for(index):
    c, _ = index
    named = shape.storage(c, host=hosts(c), names=True)
    assert [f["fileset"] for f in named["filesets"]] == ["projects", SECRET]


def test_the_default_mcp_server_never_sends_a_fileset_name(index):
    _, path = index
    hidden = tools.Catalog(index=path, host="laptop")
    shown = tools.Catalog(index=path, host="laptop", expose_names=True)
    assert SECRET not in json.dumps(hidden.storage())
    assert SECRET in json.dumps(shown.storage())
    assert {p["pool"] for p in hidden.storage()["pools"]} == {"system", "data1"}
    assert "storage" in tools.SHAPE_TOOLS


def test_mcp_find_takes_pool_and_fileset(index):
    _, path = index
    cat = tools.Catalog(index=path, host="laptop", expose_names=True)
    rows = cat.find(pool="data1")["results"]
    assert [(r["fileset"], r["pool"]) for r in rows] == [("projects", "data1")]


def test_cli_storage_find_and_stat(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["storage"]) == 0
    assert "nothing from a Storage Scale listing" in capsys.readouterr().err
    assert cli.main(["import", "--policy", str(FIXTURE)]) == 0
    capsys.readouterr()
    assert cli.main(["storage"]) == 0
    out = capsys.readouterr().out
    assert "system" in out and "data1" in out and "fileset1" in out
    assert cli.main(["storage", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pools"]
    assert cli.main(["find", "--pool", "data1", "--quiet"]) == 0
    found = capsys.readouterr().out.split()
    assert len(found) == 2
    assert cli.main(["stat", found[0]]) == 0
    assert "fileset   fileset1  (pool data1)" in capsys.readouterr().out
