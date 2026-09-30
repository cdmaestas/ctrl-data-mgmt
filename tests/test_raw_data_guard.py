"""The guard that keeps raw cluster data out of the repository.

Every raw line below is assembled at run time, so this file never itself
contains text that looks like a policy listing to the guard it tests.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_raw_data.py"
_spec = importlib.util.spec_from_file_location("check_raw_data", SCRIPT)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def listing(n: int = 5) -> bytes:
    """n lines shaped like mmapplypolicy LIST output (inode gen snapid fields -- path)."""
    return "".join(f"{1000 + i} {i} 0  {4096 * i} -- /gpfs/fs1/proj/file{i}\n"
                   for i in range(n)).encode()


def sanitized(body: bytes = b"") -> bytes:
    return guard.HEADER.encode() + b"1\n" + body


# --- the rules ---------------------------------------------------------------

@pytest.mark.parametrize("path", ["scan.list.raw", "tests/fixtures/raw/x.list",
                                  "dump.cdm-export.ndjson", "a/b/host1.cdm-export"])
def test_raw_names_and_places_are_refused(path):
    assert guard.problems(path, b"anything")


def test_a_fixture_needs_the_sanitized_header():
    assert guard.problems("tests/fixtures/fs1.list", b"no header here")
    assert not guard.problems("tests/fixtures/fs1.list", sanitized(b"x\n"))


def test_the_fixtures_readme_is_exempt():
    assert not guard.problems("tests/fixtures/README.md", b"# Test fixtures\n")


def test_a_raw_listing_under_an_innocent_name_is_caught_anywhere():
    assert guard.problems("docs/notes.txt", listing())
    assert not guard.problems("docs/notes.txt", listing(guard.LIST_LINES_TO_FLAG - 1))
    # Sanitizer output is expected to look like a listing.
    assert not guard.problems("tests/fixtures/fs1.list", sanitized(listing()))


def test_binary_files_are_not_scanned_for_listings():
    assert not guard.problems("cdm/x.bin", b"\0" + listing())


def test_ordinary_code_passes():
    assert not guard.problems("cdm/scan.py", SCRIPT.read_bytes())


# --- git integration ------------------------------------------------------------

def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def run_guard(repo, mode):
    return subprocess.run([sys.executable, str(SCRIPT), mode], cwd=repo,
                          capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "test@example.invalid")
    git(r, "config", "user.name", "test")
    (r / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    (r / "ok.py").write_text("print('hi')\n")
    git(r, "add", ".gitignore", "ok.py")
    return r


def test_gitignore_keeps_raw_files_out_of_a_plain_add(repo):
    (repo / "tests" / "fixtures" / "raw").mkdir(parents=True)
    for name in ("tests/fixtures/raw/fs1.list", "fs1.list.raw", "host1.cdm-export.ndjson"):
        (repo / name).write_bytes(listing())
    git(repo, "add", "-A")
    staged = git(repo, "diff", "--cached", "--name-only").split()
    assert staged == [".gitignore", "ok.py"]


def test_git_add_force_is_caught_at_commit_time(repo):
    (repo / "fs1.list.raw").write_bytes(listing())
    git(repo, "add", "-f", "fs1.list.raw")
    result = run_guard(repo, "--staged")
    assert result.returncode == 1 and "fs1.list.raw" in result.stderr


def test_a_renamed_raw_listing_is_caught_at_commit_time(repo):
    (repo / "notes.txt").write_bytes(listing())
    git(repo, "add", "notes.txt")
    result = run_guard(repo, "--staged")
    assert result.returncode == 1 and "LIST output" in result.stderr


def test_the_staged_content_is_checked_not_the_working_copy(repo):
    """Cleaning the file on disk after `git add` does not clean the commit."""
    (repo / "notes.txt").write_bytes(listing())
    git(repo, "add", "notes.txt")
    (repo / "notes.txt").write_text("clean now\n")
    assert run_guard(repo, "--staged").returncode == 1


def test_no_verify_is_caught_by_the_all_check(repo):
    (repo / "fs1.list.raw").write_bytes(listing())
    git(repo, "add", "-f", "fs1.list.raw")
    git(repo, "commit", "-q", "--no-verify", "-m", "oops")
    result = run_guard(repo, "--all")
    assert result.returncode == 1 and "fs1.list.raw" in result.stderr


def test_a_clean_repo_passes(repo):
    assert run_guard(repo, "--staged").returncode == 0
    git(repo, "commit", "-q", "-m", "clean")
    assert run_guard(repo, "--all").returncode == 0


def test_bad_usage_is_an_error_not_a_pass():
    assert guard.main([]) == 2 and guard.main(["--staged", "--all"]) == 2


# --- this repository ------------------------------------------------------------

def test_this_repository_is_clean():
    result = run_guard(ROOT, "--all")
    assert result.returncode == 0, result.stderr


def test_the_hook_runs_the_guard_before_it_can_skip_anything():
    hook = (ROOT / ".githooks" / "pre-commit").read_text()
    assert hook.index("check_raw_data.py --staged") < hook.index('-c "import cdm"')


def test_ci_runs_the_guard_on_everything_tracked():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "scripts/check_raw_data.py --all" in ci
    assert os.access(SCRIPT, os.X_OK)
