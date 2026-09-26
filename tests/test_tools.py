"""The MCP tool catalogue, tested without the MCP SDK.

These run on every supported Python, including 3.9 where the SDK cannot be
installed. They cover the three promises the server makes: it cannot write, it
does not leak names unless asked to, and it refuses what it cannot answer
instead of answering wrongly.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3

import pytest

from cdm import cli, db, hashing, tools
from cdm.scan import scan_root

HOST = "testhost"

# Distinctive strings placed in file and directory names below the root. None of
# them may appear anywhere in any shape tool's output. Letters only: a digit
# token could match a timestamp's microseconds by chance and flake.
SECRETS = ("bluebird", "quokka", "johnsmith", "acquisition", "salary", "hush")


@pytest.fixture()
def indexed(tmp_path):
    root = tmp_path / "root"
    layout = {
        "codename-bluebird/q3-acquisition-memo.pdf": 3000,
        "codename-bluebird/q3-acquisition-memo-v2.pdf": 3000,
        "patient-quokka/scan-001.dcm": 9000,
        "patient-quokka/scan-002.dcm": 9000,
        "JohnSmith-salary.xlsx": 400,
        "notes.JohnSmith": 50,
        "hush/hush.hush": 10,
    }
    for rel, size in layout.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"z" * size)
    index = tmp_path / "index.db"
    conn = db.connect(index)
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    conn.close()
    return tools.Catalog(index=index, host=HOST), root.resolve(), index


def call_all_shape_tools(catalog, root=None):
    return {name: getattr(catalog, name)(root=root) for name in tools.SHAPE_TOOLS}


# --- names never leak from shape tools --------------------------------------

@pytest.mark.parametrize("scoped", [False, True])
def test_shape_tools_never_return_names_below_a_root(indexed, scoped):
    catalog, root, _ = indexed
    out = json.dumps(call_all_shape_tools(catalog, str(root) if scoped else None))
    lowered = out.lower()
    leaked = [s for s in SECRETS if s in lowered]
    assert not leaked, f"shape tools leaked names: {leaked}"
    # And no path below the root in any form.
    for p in root.rglob("*"):
        assert str(p) not in out, f"shape tools leaked a path: {p}"


def test_shape_tools_do_report_the_root_itself(indexed):
    """Roots are shape-level: the user typed them."""
    catalog, root, _ = indexed
    assert str(root) in json.dumps(catalog.summary())


def test_a_repeated_short_suffix_is_shown_as_an_extension(indexed, tmp_path):
    """Documents where the line is, deliberately.

    Two files sharing a short alphanumeric suffix are treated as a file type.
    If that suffix is really a word from their names, it is shown. This is the
    residual limit of the extension filter, recorded in docs/adr/0002 -- the
    test exists so the behaviour is a decision rather than a surprise.
    """
    catalog, root, _ = indexed
    exts = {e["extension"] for e in catalog.extensions()["extensions"]}
    assert "dcm" in exts and "pdf" in exts
    assert "hush" not in exts       # only one .hush file: folded into (other)


def test_shape_tools_take_no_path_arguments(indexed):
    """Structural guard: a shape tool that accepted a path could confirm names."""
    catalog, _, _ = indexed
    for name in tools.SHAPE_TOOLS:
        params = set(inspect.signature(getattr(catalog, name)).parameters)
        # older_than_days is an int threshold for `suggest`: it cannot carry a path.
        assert params <= {"root", "limit", "older_than_days"}, f"{name} takes {params}"


def test_tool_sets_are_disjoint_and_all_implemented():
    assert not set(tools.SHAPE_TOOLS) & set(tools.NAME_TOOLS)
    for name in (*tools.SHAPE_TOOLS, *tools.NAME_TOOLS):
        assert callable(getattr(tools.Catalog, name))


def test_name_tools_are_only_listed_when_exposed():
    assert tools.tool_names(False) == list(tools.SHAPE_TOOLS)
    assert set(tools.tool_names(True)) == set(tools.SHAPE_TOOLS) | set(tools.NAME_TOOLS)


# --- read-only --------------------------------------------------------------

def test_readonly_connection_refuses_writes(indexed):
    _, _, index = indexed
    conn = db.connect_readonly(index)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM files")
    conn.close()


def test_every_tool_leaves_the_index_byte_for_byte_unchanged(indexed):
    catalog, root, index = indexed
    before = hashlib.sha256(index.read_bytes()).hexdigest()
    call_all_shape_tools(catalog)
    catalog.find(name="*.pdf")
    catalog.du(str(root))
    catalog.dupes()
    catalog.stat(str(root / "hush" / "hush.hush"))
    catalog.banner(expose_names=True)
    assert hashlib.sha256(index.read_bytes()).hexdigest() == before


def test_readonly_open_handles_awkward_paths(tmp_path):
    odd = tmp_path / "with space & 100%"
    odd.mkdir()
    conn = db.connect(odd / "index.db")
    conn.close()
    ro = db.connect_readonly(odd / "index.db")
    assert ro.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    ro.close()


def test_missing_index_is_reported_not_created(tmp_path):
    with pytest.raises(db.IndexUnavailable, match="cdm scan"):
        db.connect_readonly(tmp_path / "absent.db")
    assert not (tmp_path / "absent.db").exists()


def test_old_schema_is_refused_with_the_fix(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    with pytest.raises(db.IndexUnavailable, match="cdm doctor"):
        db.connect_readonly(path)


# --- refusing rather than answering wrongly --------------------------------

def test_unknown_root_is_refused_with_the_real_ones(indexed):
    catalog, root, _ = indexed
    with pytest.raises(ValueError) as exc:
        catalog.summary(root="/somewhere/else")
    assert str(root) in str(exc.value)


def test_root_accepted_in_either_spelling(indexed, tmp_path):
    catalog, root, _ = indexed
    assert catalog.summary(root=str(root))["roots"]
    unresolved = tmp_path / "root"          # may differ from root on macOS
    assert catalog.summary(root=str(unresolved))["roots"]


def test_find_validates_its_enums(indexed):
    catalog, _, _ = indexed
    with pytest.raises(ValueError):
        catalog.find(kind="socket")
    with pytest.raises(ValueError):
        catalog.find(order="random")


def test_find_caps_and_reports_truncation(indexed):
    catalog, _, _ = indexed
    out = catalog.find(kind="file", limit=2)
    assert len(out["results"]) == 2
    assert out["truncated"] is True


# --- name tools, when exposed -----------------------------------------------

def test_name_tools_return_paths(indexed):
    catalog, root, _ = indexed
    found = catalog.find(iname="*.PDF")["results"]
    assert len(found) == 2 and all("acquisition" in f["path"] for f in found)
    assert catalog.stat(str(root / "notes.JohnSmith"))["found"] is True
    assert catalog.stat("/not/indexed")["found"] is False
    assert catalog.du(str(root))["results"]
    assert any(g["paths"] for g in catalog.dupes()["groups"])


# --- banner ------------------------------------------------------------------

def test_banner_says_what_is_exposed(indexed):
    catalog, root, _ = indexed
    closed = "\n".join(catalog.banner(expose_names=False))
    assert "NOT exposed" in closed and str(root) in closed
    assert "find" not in closed.split("tools:")[1].splitlines()[0]
    opened = "\n".join(catalog.banner(expose_names=True))
    assert "WARNING --expose-names" in opened


def test_banner_warns_on_a_readable_index_without_fixing_it(indexed):
    """A read-only server reports permissions; it does not chmod anything."""
    catalog, _, index = indexed
    os.chmod(index, 0o644)
    assert any("readable by other users" in line
               for line in catalog.banner(expose_names=False))
    assert os.stat(index).st_mode & 0o777 == 0o644


# --- the CLI entry point, without the SDK -----------------------------------

def test_cdm_mcp_explains_a_missing_extra(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_mcp_server", lambda: None)
    monkeypatch.setattr(cli.sys, "version_info", (3, 12, 0))
    assert cli.main(["mcp"]) == 2
    err = capsys.readouterr()
    assert "pipx inject ctrl-data-mgmt mcp" in err.err
    assert err.out == "", "cdm mcp wrote to stdout, which is the protocol channel"


def test_cdm_mcp_explains_python_39(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "version_info", (3, 9, 18))
    assert cli.main(["mcp"]) == 2
    assert "3.10" in capsys.readouterr().err


def test_an_unrelated_import_error_is_not_disguised(monkeypatch):
    """Only a missing `mcp` package means 'extra not installed'."""
    import builtins
    real_import = builtins.__import__

    def broken(name, globals=None, locals=None, fromlist=(), level=0):
        # `from . import mcp_server` arrives as name='' with the module in fromlist.
        if "mcp_server" in (fromlist or ()) or name.endswith("mcp_server"):
            raise ImportError("something else is broken", name="yaml")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", broken)
    with pytest.raises(ImportError, match="something else"):
        cli._load_mcp_server()
