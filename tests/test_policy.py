"""Storage Scale policy listings: the generated rules and script, and the reader.

The formats here were measured on a real Storage Scale cluster (see
cdm/policy.py). The script tests run the generated shell code for real against
stand-in mm* commands that reproduce those formats, so the header, the end
marker and the refusals are exercised on every run without a cluster.

Listing lines are assembled at run time so this file never itself looks like
raw policy output to scripts/check_raw_data.py.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from urllib.parse import quote

import pytest

from cdm import cli, policy

UTC = timezone.utc


def show(size=1, mtime="2026-01-15 12:00:00.000000", atime="2025-06-01 00:00:00.000000",
         ctime="2026-09-30 02:26:02.433582", uid=1000, gid=1000, mode="-rw-r--r--",
         fileset="proj", pool="system", nlink=1) -> str:
    fields = [str(size), mtime, atime, ctime, str(uid), str(gid), mode, fileset, pool,
              str(nlink)]
    return quote("|".join(fields), safe="")


def line(path: str, inode=544769, **kw) -> str:
    # ESCAPE '%/': the path keeps its slashes and nothing else special.
    return f"{inode} 124953298 0  {show(**kw)} -- {quote(path.encode(), safe='/')}\n"


HEADER = [f"# {policy.FORMAT}\n", "# device: fs1\n", "# cluster: cluster1.example\n",
          "# scope: fileset proj\n", "# suppress_atime: relatime\n",
          f"# fields: {'|'.join(policy.FIELDS)}\n", "# times: utc\n",
          "# generated: 2026-09-30T02:29:03Z\n", "# generator: cdm test\n"]


def listing(*entries: str, end: int | None = None) -> list[str]:
    body = list(entries)
    tail = [f"{policy.END}{len(body) if end is None else end}\n"] if end != -1 else []
    return HEADER + body + tail


# --- the rules ----------------------------------------------------------------

def test_rules_list_one_fileset_with_every_field_escaped():
    text = policy.rules("proj")
    assert "RULE EXTERNAL LIST 'cdm' EXEC '' ESCAPE '%/'" in text
    assert "DIRECTORIES_PLUS FOR FILESET('proj')" in text
    order = [text.index(policy._SHOW[f]) for f in policy.FIELDS]
    assert order == sorted(order), "SHOW() fields out of the documented order"


def test_whole_filesystem_rules_have_no_fileset_clause():
    assert "FOR FILESET" not in policy.rules(None)


def test_no_function_the_policy_engine_lacks():
    """Measured: Storage Scale's policy SQL has no MIDNIGHT_SECONDS()."""
    assert "MIDNIGHT_SECONDS" not in policy.rules("proj")


@pytest.mark.parametrize("bad", ["fs1;rm -rf /", "a'b", "a b", "$(id)", "", "-x", "a\nb"])
def test_names_that_could_escape_the_script_or_the_sql_are_refused(bad):
    with pytest.raises(policy.PolicyError):
        policy.script(bad, "proj")
    with pytest.raises(policy.PolicyError):
        policy.script("fs1", bad)


def test_the_listing_must_be_a_raw_file():
    with pytest.raises(policy.PolicyError, match=r"\.raw"):
        policy.script("fs1", "proj", output="fs1.list")


# --- the script, run for real against stand-in mm* commands -------------------

FAKE_LSCLUSTER = """#!/bin/sh
echo 'mmlscluster:clusterSummary:HEADER:version:reserved:reserved:clusterName:clusterId:'
echo 'mmlscluster:clusterSummary:0:1:::cluster1.example:123456789:'
echo 'mmlscluster:clusterNode:HEADER:version:reserved:reserved:nodeNumber:'
"""
FAKE_LSFS = """#!/bin/sh
echo 'mmlsfs::HEADER:version:reserved:reserved:deviceName:fieldName:data:remarks:'
echo "mmlsfs::0:1:::$1:suppressAtime:relatime::"
"""
FAKE_APPLY = """#!/bin/sh
# mmapplypolicy DEVICE -P RULES -I defer -f PREFIX [...]: write PREFIX.list.cdm
echo "$@" > "$FAKE_DIR/args"
cp "$2" "$FAKE_DIR/rules.seen" 2>/dev/null || cp "$3" "$FAKE_DIR/rules.seen"
prefix=""; while [ $# -gt 0 ]; do [ "$1" = -f ] && prefix="$2"; shift; done
[ -n "${FAKE_FAIL:-}" ] && { echo "[E] simulated failure"; exit 1; }
[ -f "$FAKE_DIR/listing" ] && cp "$FAKE_DIR/listing" "$prefix.list.cdm"
exit 0
"""


@pytest.fixture()
def fake_cluster(tmp_path):
    if shutil.which("sh") is None:
        pytest.skip("no POSIX sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("mmlscluster", FAKE_LSCLUSTER), ("mmlsfs", FAKE_LSFS),
                       ("mmapplypolicy", FAKE_APPLY)):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    return tmp_path


def run_script(fake, text, *args, **env):
    script = fake / "list.sh"
    script.write_text(text)
    environ = dict(os.environ, PATH=f"{fake / 'bin'}:{os.environ['PATH']}",
                   FAKE_DIR=str(fake), TMPDIR=str(fake), **env)
    return subprocess.run(["sh", str(script), *args], cwd=fake, env=environ,
                          capture_output=True, text=True)


def test_the_script_writes_a_complete_private_listing(fake_cluster):
    rows = [line("/gpfs/fs1/proj/a b.txt"), line("/gpfs/fs1/proj/dir", mode="drwxr-xr-x")]
    (fake_cluster / "listing").write_text("".join(rows))
    result = run_script(fake_cluster, policy.script("fs1", "proj"))
    assert result.returncode == 0, result.stderr
    out = fake_cluster / "fs1-proj.list.raw"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    with out.open() as f:
        header, entries, state = policy.read_listing(f)
        got = list(entries)
    assert header["cluster"] == "cluster1.example" and header["suppress_atime"] == "relatime"
    assert header["scope"] == "fileset proj" and state["complete"]
    assert [e.path for e in got] == ["/gpfs/fs1/proj/a b.txt", "/gpfs/fs1/proj/dir"]
    assert "-I defer" in (fake_cluster / "args").read_text()
    assert (fake_cluster / "rules.seen").read_text() == policy.rules("proj")
    # Its work directory is removed, so no copy of the listing is left behind.
    assert not list(fake_cluster.glob("cdm-policy.*"))


def test_an_empty_scope_is_a_complete_empty_listing(fake_cluster):
    result = run_script(fake_cluster, policy.script("fs1", "proj"))
    assert result.returncode == 0, result.stderr
    with (fake_cluster / "fs1-proj.list.raw").open() as f:
        _, entries, state = policy.read_listing(f)
        assert list(entries) == [] and state["complete"]


def test_a_failed_policy_run_leaves_nothing_that_looks_whole(fake_cluster):
    (fake_cluster / "listing").write_text(line("/gpfs/fs1/proj/x"))
    result = run_script(fake_cluster, policy.script("fs1", "proj"), FAKE_FAIL="1")
    assert result.returncode == 1 and "simulated failure" in result.stderr
    assert not list(fake_cluster.glob("*.raw*"))


def test_the_output_can_be_named_but_must_stay_raw(fake_cluster):
    text = policy.script("fs1", "proj")
    assert run_script(fake_cluster, text, "mine.list.raw").returncode == 0
    assert (fake_cluster / "mine.list.raw").exists()
    refused = run_script(fake_cluster, text, "mine.txt")
    assert refused.returncode == 2 and not (fake_cluster / "mine.txt").exists()


def test_nodes_are_passed_to_mmapplypolicy(fake_cluster):
    run_script(fake_cluster, policy.script("fs1", None, nodes="nsdNodes"))
    assert "-N nsdNodes" in (fake_cluster / "args").read_text()


# --- reading ------------------------------------------------------------------

def test_escaped_paths_and_fields_decode_exactly():
    names = ["with space.txt", "pct%41.txt", "café.txt", "tab\tname.txt", "sub dir/inner"]
    for name in names:
        e = policy.parse_entry(line(f"/gpfs/fs1/proj/{name}"))
        assert e.path == f"/gpfs/fs1/proj/{name}"


def test_times_are_utc_whatever_the_local_zone():
    e = policy.parse_entry(line("/gpfs/fs1/proj/x", mtime="2026-01-15 12:00:00.000000",
                                atime="2026-07-04 18:30:15.250000"))
    assert e.mtime == datetime(2026, 1, 15, 12, tzinfo=UTC).timestamp()
    assert e.atime == datetime(2026, 7, 4, 18, 30, 15, 250000, tzinfo=UTC).timestamp()


def test_fields_and_kinds():
    e = policy.parse_entry(line("/gpfs/fs1/proj/x", size=4096, uid=1001, gid=100,
                                mode="drwxr-xr-x", pool="data1", nlink=2))
    assert (e.size, e.uid, e.gid, e.pool, e.nlink, e.kind) == (4096, 1001, 100, "data1", 2,
                                                               "dir")
    assert policy.parse_entry(line("/x/l", mode="lrwxrwxrwx")).kind == "link"
    assert policy.parse_entry(line("/x/f")).kind == "file"


@pytest.mark.parametrize("bad", ["not a listing line\n", "1 2 3  x -- /p\n",
                                 "1 2 3 -- /p\n"])
def test_malformed_lines_are_refused(bad):
    with pytest.raises(policy.ListingError):
        policy.parse_entry(bad)


def test_a_complete_listing_says_so():
    header, entries, state = policy.read_listing(listing(line("/a"), line("/b")))
    assert len(list(entries)) == 2 and state["complete"] and header["device"] == "fs1"


def test_a_truncated_listing_is_not_complete():
    _, entries, state = policy.read_listing(listing(line("/a"), line("/b"), end=-1))
    assert len(list(entries)) == 2 and not state["complete"]


def test_an_end_marker_that_disagrees_is_an_error():
    _, entries, _ = policy.read_listing(listing(line("/a"), end=5))
    with pytest.raises(policy.ListingError, match="damaged"):
        list(entries)


def test_data_after_the_end_marker_is_an_error():
    _, entries, _ = policy.read_listing(listing(line("/a")) + [line("/b")])
    with pytest.raises(policy.ListingError, match="after the end"):
        list(entries)


def test_a_file_that_is_not_a_cdm_listing_is_refused():
    with pytest.raises(policy.ListingError, match="cdm policy"):
        policy.read_listing([line("/a")])


def test_a_listing_with_other_fields_is_refused():
    bad = [h.replace("|nlink", "") if h.startswith("# fields") else h for h in HEADER]
    with pytest.raises(policy.ListingError, match="unsupported fields"):
        policy.read_listing(bad + [f"{policy.END}0\n"])


# --- the command --------------------------------------------------------------

def test_cdm_policy_prints_the_script_and_advice_separately(capsys):
    assert cli.main(["policy", "--device", "fs1", "--fileset", "proj"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("#!/bin/sh") and "FOR FILESET('proj')" in out
    assert "as root" in err and "WARNING" not in err


def test_the_whole_filesystem_needs_asking_for_and_warns(capsys):
    with pytest.raises(SystemExit):
        cli.main(["policy", "--device", "fs1"])
    capsys.readouterr()
    assert cli.main(["policy", "--device", "fs1", "--whole-filesystem"]) == 0
    assert "every user's files" in capsys.readouterr().err


def test_cdm_policy_refuses_unsafe_names(capsys):
    assert cli.main(["policy", "--device", "fs1;id", "--fileset", "proj"]) == 2
    assert "not a plain Storage Scale name" in capsys.readouterr().err
