"""Suggestions: what each rule finds, what it refuses to find, and what it says.

Thresholds are lowered per test instead of writing gigabyte fixtures; the rules
read them at call time.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from cdm import cli, db, hashing, suggest, tools
from cdm.scan import scan_root

HOST = "testhost"
DAY = 86400
NOW = time.time()
OLD = NOW - 400 * DAY


@pytest.fixture(autouse=True)
def small_thresholds(monkeypatch):
    for name in ("MIN_KNOWN_CACHE", "MIN_GENERIC_CACHE", "MIN_GIT", "MIN_INSTALLER",
                 "MIN_MODEL_FILE", "MIN_DUPLICATES"):
        monkeypatch.setattr(suggest, name, 100)


def write(root, rel, size=200, mtime=None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(size))
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def run(tmp_path, root, *, checksum=True, names=True, now=NOW, **kw):
    conn = db.connect(tmp_path / "index.db")
    try:
        scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL if checksum else None)
        return suggest.suggest(conn, host=HOST, names=names, now=now, **kw)
    finally:
        conn.close()


def by_id(result, prefix):
    return [s for s in result["suggestions"] if s["id"].startswith(prefix)]


# --- caches -----------------------------------------------------------------

def test_a_known_cache_is_safe_with_its_own_command(tmp_path):
    root = tmp_path / "home"
    write(root, ".npm/_cacache/content/aa", 300)
    [s] = by_id(run(tmp_path, root), "cache:npm")
    assert s["risk"] == suggest.SAFE
    assert s["action"] == "npm cache clean --force"
    assert s["bytes"] == 300


def test_an_enclosing_cache_is_not_credited_with_what_is_listed_inside_it(tmp_path):
    root = tmp_path / "home"
    write(root, "Library/Caches/Homebrew/downloads/x.tar.gz", 500)
    write(root, "Library/Caches/com.example.app/blob", 300)
    result = run(tmp_path, root)
    [brew] = by_id(result, "cache:Homebrew")
    [mac] = by_id(result, "cache:macOS")
    assert brew["bytes"] == 500
    assert mac["bytes"] == 300, "Homebrew's bytes were counted twice"


def test_sandboxed_app_caches_are_left_to_macos(tmp_path):
    root = tmp_path / "home"
    write(root, "Library/Containers/com.apple.x/Data/Library/Caches/blob", 300)
    assert not by_id(run(tmp_path, root), "cache:macOS")


# --- dependencies and build output --------------------------------------------

def checkout(root, rel="proj"):
    (root / rel / ".git").mkdir(parents=True)
    write(root, f"{rel}/.git/HEAD", 20)
    write(root, f"{rel}/package.json", 20)
    return root / rel


def test_stale_dependencies_in_a_checkout_are_safe_to_remove(tmp_path):
    root = tmp_path / "home"
    checkout(root)
    write(root, "proj/node_modules/lib/index.js", 400, mtime=OLD)
    write(root, "proj/node_modules/lib/node_modules/dep/x.js", 100, mtime=OLD)
    [s] = by_id(run(tmp_path, root), "build:rebuildable")
    assert s["risk"] == suggest.SAFE
    # Only the outermost node_modules: the nested one is inside it.
    assert [i["path"] for i in s["items"]] == [str((root / "proj/node_modules").resolve())]
    assert s["items"][0]["bytes"] == 500


def test_recently_touched_dependencies_are_left_alone(tmp_path):
    root = tmp_path / "home"
    checkout(root)
    write(root, "proj/node_modules/lib/index.js", 400)
    assert not by_id(run(tmp_path, root), "build:")


def test_nothing_outside_a_checkout_is_called_rebuildable(tmp_path):
    """An installed editor extension has package.json and node_modules too."""
    root = tmp_path / "home"
    write(root, ".vscode/extensions/ext-1.0/package.json", 20, mtime=OLD)
    write(root, ".vscode/extensions/ext-1.0/node_modules/x.js", 400, mtime=OLD)
    write(root, ".vscode/extensions/ext-1.0/dist/main.js", 400, mtime=OLD)
    assert not by_id(run(tmp_path, root), "build:")


def test_build_output_needs_review_not_just_a_rebuild(tmp_path):
    root = tmp_path / "home"
    checkout(root)
    write(root, "proj/dist/app.dmg", 400, mtime=OLD)
    [s] = by_id(run(tmp_path, root), "build:output")
    assert s["risk"] == suggest.REVIEW


def test_a_large_git_history_is_suggested_for_repacking(tmp_path):
    root = tmp_path / "home"
    checkout(root)
    write(root, "proj/.git/objects/pack/p.pack", 500)
    [s] = by_id(run(tmp_path, root), "git:large")
    assert s["items"][0]["action"].startswith("git -C ")
    assert "gc" in s["items"][0]["action"]


# --- files --------------------------------------------------------------------

def test_old_installers_and_downloaded_archives(tmp_path):
    root = tmp_path / "home"
    write(root, "Downloads/Tool-1.0.dmg", 300, mtime=OLD)
    write(root, "Downloads/data.zip", 300, mtime=OLD)
    write(root, "Downloads/fresh.dmg", 300)
    write(root, "work/archive.zip", 300, mtime=OLD)   # not a download: data
    [s] = by_id(run(tmp_path, root), "files:installers")
    names = sorted(os.path.basename(i["path"]) for i in s["items"])
    assert names == ["Tool-1.0.dmg", "data.zip"]


def test_model_files_are_grouped_by_store_and_never_called_safe(tmp_path):
    root = tmp_path / "home"
    write(root, ".ollama/models/blobs/sha256-aaa", 300)
    write(root, ".ollama/models/blobs/sha256-bbb", 300)
    write(root, "weights/llama.gguf", 300)
    [s] = by_id(run(tmp_path, root), "files:models")
    assert s["risk"] == suggest.REVIEW
    stores = {os.path.basename(i["path"]): i for i in s["items"]}
    assert stores["models"]["files"] == 2 and stores["models"]["bytes"] == 600
    assert "weights" in stores
    assert "last read" in s["detail"]


def test_duplicates_point_at_verification(tmp_path):
    root = tmp_path / "home"
    p = write(root, "a/one.bin", 400)
    (root / "b").mkdir()
    (root / "b/two.bin").write_bytes(p.read_bytes())
    [s] = by_id(run(tmp_path, root), "dupes")
    assert s["bytes"] == 400
    assert "--verify" in s["action"]


# --- housekeeping and ordering ------------------------------------------------

def test_an_old_scan_is_the_first_thing_suggested(tmp_path):
    root = tmp_path / "home"
    write(root, ".npm/_cacache/x", 300)
    result = run(tmp_path, root, now=NOW + 30 * DAY)
    first = result["suggestions"][0]
    assert first["id"].startswith("index.stale:")
    assert first["action"] == f"cdm rescan {root.resolve()}"


def test_an_unhashed_root_is_told_to_checksum(tmp_path):
    root = tmp_path / "home"
    write(root, "a.txt", 10)
    [s] = by_id(run(tmp_path, root, checksum=False), "index.unhashed")
    assert s["action"] == f"cdm rescan --checksum {root.resolve()}"
    assert s["risk"] == suggest.NONE


def test_suggestions_are_ranked_by_size_after_housekeeping(tmp_path):
    root = tmp_path / "home"
    write(root, ".npm/_cacache/x", 300)
    write(root, ".cache/uv/y", 900)
    sizes = [s["bytes"] for s in run(tmp_path, root)["suggestions"]]
    assert sizes == sorted(sizes, reverse=True)


def test_nothing_to_suggest_is_an_empty_list(tmp_path):
    root = tmp_path / "home"
    write(root, "a.txt", 10)
    assert run(tmp_path, root)["suggestions"] == []


# --- names --------------------------------------------------------------------

def test_without_names_no_path_below_the_root_is_returned(tmp_path):
    root = tmp_path / "home"
    checkout(root, "codename-bluebird")
    write(root, "codename-bluebird/node_modules/x.js", 400, mtime=OLD)
    write(root, "Downloads/quokka-installer.dmg", 300, mtime=OLD)
    write(root, "models/johnsmith.gguf", 300)
    result = run(tmp_path, root, names=False)
    text = json.dumps(result)
    assert result["suggestions"], "nothing found; the test proves nothing"
    for secret in ("bluebird", "quokka", "johnsmith", "Downloads", "node_modules/"):
        assert secret not in text, secret
    for s in result["suggestions"]:
        assert "items" not in s
        assert s["action"], f"{s['id']} tells the user nothing to do"


# --- front-ends ---------------------------------------------------------------

def test_cli_suggest_prints_and_changes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CDM_HOST", HOST)
    root = tmp_path / "home"
    cache = write(root, ".npm/_cacache/x", 300)
    assert cli.main(["scan", str(root), "--checksum"]) == 0
    capsys.readouterr()

    assert cli.main(["suggest"]) == 0
    out, err = capsys.readouterr()
    assert "Clear npm cache" in out and "$ npm cache clean --force" in out
    assert "nothing has been changed" in err
    assert cache.exists()

    assert cli.main(["suggest", "--json", "--older-than", "30d"]) == 0
    assert json.loads(capsys.readouterr().out)["suggestions"][0]["id"] == "cache:npm cache"


def test_cli_suggest_rejects_a_bad_day_count(capsys):
    with pytest.raises(SystemExit):
        cli.main(["suggest", "--older-than", "soon"])


def test_every_catalog_tool_says_where_to_go_next(tmp_path):
    root = tmp_path / "home"
    write(root, ".npm/_cacache/x", 300)
    index = tmp_path / "index.db"
    conn = db.connect(index)
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    conn.close()
    for expose in (False, True):
        catalog = tools.Catalog(index=index, host=HOST, expose_names=expose)
        for name in tools.tool_names(expose):
            fn = getattr(catalog, name)
            args = {"path": str(root)} if name in ("du", "stat") else {}
            if name == "stat":
                continue   # one path's record; nowhere obvious to go next
            out = fn(**args)
            assert out.get("next_steps"), f"{name} (names={expose}) has no next_steps"


def test_catalog_suggest_follows_the_names_switch(tmp_path):
    root = tmp_path / "home"
    write(root, "Downloads/quokka.dmg", 300, mtime=OLD)
    index = tmp_path / "index.db"
    conn = db.connect(index)
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    conn.close()
    hidden = tools.Catalog(index=index, host=HOST).suggest()
    shown = tools.Catalog(index=index, host=HOST, expose_names=True).suggest()
    assert "quokka" not in json.dumps(hidden)
    assert "quokka" in json.dumps(shown)


@pytest.mark.parametrize("name", list(tools.PROMPTS))
def test_prompts_name_only_tools_that_exist(name):
    """A prompt must never send the model to a tool it does not have."""
    without = tools.prompt_text(name, expose_names=False)
    for tool in tools.NAME_TOOLS:
        assert f"`{tool}`" not in without, f"{name} mentions {tool} without names"
    assert tools.prompt_text(name, expose_names=False, root="/r").count("root='/r'") \
        == (1 if "{scope}" in tools.PROMPTS[name][2] else 0)
