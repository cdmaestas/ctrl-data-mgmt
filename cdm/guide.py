"""The guide: a fixed, ordered set of steps from install to a well-kept index.

Like suggest.py, this is one engine that every front-end renders -- `cdm guide`
prints it as a checklist, the MCP `guide` tool and `getting-started` prompt walk
a user through it, and a UI shows it as a wizard. See docs/adr/0004.

Two rules make it trustworthy:

* STATUS COMES FROM THE INDEX. A step is `done` because the index shows it --
  a root exists, its files are hashed, its last scan is recent -- never because
  someone clicked Next. Steps the index cannot observe (whether you acted on a
  suggestion, whether an AI client is connected) are `advice` or `optional`,
  and never claim to be done.
* ADVISORY. Each step carries the command to run and what you will see after.
  Nothing here runs a command, and anything that installs something lasting (a
  scheduled rescan) is printed for a person to install.

Root paths appear (the user chose them); nothing below a root does, so the
guide is safe to return with names off.
"""
from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import roots as roots_mod
from . import suggest as suggest_mod
from .shape import DAY, human

DONE, TODO, ADVICE, OPTIONAL = "done", "todo", "advice", "optional"
FRESH_DAYS = 7
MIN_COVERAGE = 0.9
LAUNCHD_LABEL = "io.github.ctrl-data-mgmt.rescan"
CHECKABLE = ("index", "hash", "fresh")


@dataclass
class Step:
    id: str
    title: str
    status: str
    why: str
    command: str
    expect: str
    detail: str = ""


def _roots(conn, host: str, root: str | None):
    where, params = "host = ?", [host]
    if root is not None:
        where += " AND " + roots_mod.scope_sql("path")
        params += roots_mod.scope_params(root)
    return conn.execute(
        f"SELECT path, last_scan FROM roots WHERE {where} ORDER BY path", params
    ).fetchall()


def _coverage(conn, host: str, path: str) -> tuple[int, int]:
    """(files, hashed) owned by one root."""
    r = conn.execute(
        "SELECT COUNT(*), COUNT(hash) FROM files WHERE host = ? AND root = ? "
        "AND type = 'file'", (host, path)).fetchone()
    return r[0], r[1]


def guide(conn, *, host: str, root: str | None = None, now: float | None = None,
          fresh_days: int = FRESH_DAYS) -> dict:
    """The steps, each with a status observed from the index, and which is next."""
    now = time.time() if now is None else now
    roots = _roots(conn, host, root)
    steps: list[Step] = []

    # 1. Index something.
    steps.append(Step(
        "index", "Index a folder", DONE if roots else TODO,
        "Everything cdm answers comes from its index, and it indexes only the "
        "folders you name -- there is no default crawl.",
        "cdm scan ~/work --checksum",
        "A line per root with files, directories and the scan rate. Your home "
        "directory is a fine root; it is simply never picked for you.",
        ", ".join(r["path"] for r in roots)))

    # 2. Hash it, or duplicates and most suggestions cannot be found.
    thin = []
    total_files = total_hashed = 0
    for r in roots:
        files, hashed = _coverage(conn, host, r["path"])
        total_files += files
        total_hashed += hashed
        if files and hashed / files < MIN_COVERAGE:
            thin.append(r["path"])
    share = f"{total_hashed / total_files:.0%} of files hashed" if total_files else ""
    steps.append(Step(
        "hash", "Hash it for duplicates",
        TODO if not roots or thin else DONE,
        "A file that was never hashed cannot show up as a duplicate. --checksum "
        "reads only both ends of each file, and unchanged files are never "
        "read again.",
        f"cdm rescan --checksum {shlex.quote(thin[0])}" if thin
        else "cdm rescan --checksum",
        "'hashed N (… read, …/s), reused M unchanged'. On a rescan, nearly "
        "everything is reused.",
        share))

    # 3. Look at what is worth doing. Observable only as "is there anything".
    found = suggest_mod.suggest(conn, host=host, root=root, names=False, now=now,
                                limit=100)["suggestions"] if roots else []
    safe = [s for s in found if s["risk"] == suggest_mod.SAFE]
    review = [s for s in found if s["risk"] == suggest_mod.REVIEW]
    size = human(sum(s["bytes"] for s in found if s["risk"] != suggest_mod.NONE))
    steps.append(Step(
        "suggest", "See what's worth doing",
        TODO if not roots else (DONE if not (safe or review) else ADVICE),
        "cdm ranks caches, stale build output, old installers, large git "
        "histories, model files and duplicates by size, with a risk and the "
        "command for each. It never runs anything.",
        "cdm suggest",
        "A ranked list. Start with `safe` items; `review` means look first.",
        (f"{len(safe)} safe and {len(review)} to review, about {size}"
         if found else ("nothing to act on" if roots else ""))))

    # 4. Act, then rescan. The index cannot see that you acted, so: advice.
    steps.append(Step(
        "act", "Clean up, then rescan", ADVICE if roots else TODO,
        "After you delete anything, a rescan brings the index back in line, "
        "so later answers do not count what is gone.",
        "cdm rescan --checksum",
        "'dropped N row(s) for files no longer on disk'."))

    # 5. Keep it current.
    stale = []
    for r in roots:
        if r["last_scan"] is None:
            stale.append((r["path"], None))
            continue
        age = (now - datetime.fromisoformat(r["last_scan"]).timestamp()) / DAY
        if age >= fresh_days:
            stale.append((r["path"], age))
    oldest = max((a for _, a in stale if a is not None), default=None)
    steps.append(Step(
        "fresh", "Keep it current", TODO if not roots or stale else DONE,
        f"Answers are only as new as the last scan. A nightly rescan keeps "
        f"every root under {fresh_days} days old without you remembering.",
        "cdm guide --schedule",
        "A scheduled job to install yourself (launchd on macOS, cron "
        "elsewhere). cdm never installs it for you.",
        (f"{len(stale)} root(s) not scanned in {fresh_days}+ days"
         + (f", oldest {oldest:.0f} days" if oldest else "")) if stale
        else (f"every root scanned within {fresh_days} days" if roots else "")))

    # 6. Optional: an AI client. Not observable from the index.
    steps.append(Step(
        "ai", "Ask questions with an AI client", OPTIONAL,
        "An MCP client such as Claude Code can answer plain-language questions "
        "from the index. By default it sees totals only, never file names; "
        "`--expose-names` also shares paths with the model's provider.",
        "claude mcp add --scope user cdm -- cdm mcp",
        "Ask 'what can I clean up?', or pick the cleanup prompt from the "
        "client's menu. Needs the MCP extra: pipx inject ctrl-data-mgmt mcp."))

    # The first step not done is the next one; with everything checkable done,
    # the next useful thing is to look at suggestions again.
    nxt = next((s for s in steps if s.status == TODO), None)
    if nxt is None:
        nxt = next((s for s in steps if s.status == ADVICE), steps[2])
    # Progress counts only the steps the index can always confirm, so the
    # total does not move as the user goes: "0 of 3" becomes "3 of 3".
    checkable = [s for s in steps if s.id in CHECKABLE]
    return {
        "steps": [asdict(s) for s in steps],
        "next": nxt.id,
        "done": sum(s.status == DONE for s in checkable),
        "of": len(checkable),
        "note": ("Statuses come from the index. 'advice' and 'optional' steps "
                 "are ones the index cannot confirm, so they never show as done."),
    }


# --- scheduling ----------------------------------------------------------------

def cdm_executable() -> str:
    """An absolute path to cdm, because schedulers do not have your shell's PATH."""
    # The pipx link itself, not what it points at: the link survives a
    # reinstall, the virtualenv path behind it does not.
    found = shutil.which("cdm")
    return os.path.abspath(found) if found else ""


def _command(executable: str | None) -> list[str]:
    """The argv a scheduler runs to start cdm.

    `cdm` from PATH when there is one; otherwise this interpreter with `-m cdm`,
    which is right however cdm was started. Previously this fell back to
    sys.argv[0], which under `python -m cdm` is `.../cdm/__main__.py`: a job
    that failed every night, visible only in its log.
    """
    exe = executable if executable is not None else cdm_executable()
    return [exe] if exe else [sys.executable, "-m", "cdm"]


def schedule(platform: str | None = None, executable: str | None = None,
             hour: int = 3, minute: int = 30) -> tuple[str, str]:
    """(job, instructions) for a nightly rescan. Printed, never installed.

    Split so the job alone can be redirected into place:
    `cdm guide --schedule > ~/Library/LaunchAgents/<label>.plist`.
    """
    platform = platform or sys.platform
    argv = _command(executable)
    if platform == "darwin":
        plist = f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist"
        log = Path("~/Library/Logs/cdm-rescan.log").expanduser()
        # plistlib, not a template: it escapes by construction. A template put
        # the executable path into XML verbatim, so a path with `&` or `<` made
        # a plist launchd refuses.
        job = plistlib.dumps({
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [*argv, "rescan", "--checksum", "--progress", "60"],
            "StartCalendarInterval": {"Hour": hour, "Minute": minute},
            "LowPriorityIO": True,
            "Nice": 10,
            "StandardErrorPath": str(log),
        }).decode()
        how = f"""\
Nightly `cdm rescan --checksum` at {hour:02d}:{minute:02d} as a launchd agent. To install:
  cdm guide --schedule > {plist}
  launchctl bootstrap gui/$(id -u) {plist}
To remove:
  launchctl bootout gui/$(id -u)/{LAUNCHD_LABEL} && rm {plist}
A run missed while the Mac slept happens at next wake. Folders protected by
macOS privacy controls stay unreadable to a background job unless you grant it
Full Disk Access; {log} lists what was skipped."""
        return job, how
    # cron hands the command to a shell, so the path is quoted; and cron turns
    # an unescaped % into a newline, so those are escaped after quoting.
    # Unquoted, a path with `&` or `;` ran as more than one command.
    quoted = " ".join(shlex.quote(a) for a in argv).replace("%", "\\%")
    job = (f"{minute} {hour} * * * nice -n 10 {quoted} rescan --checksum --progress 60 "
           f'>>"$HOME/.cdm-rescan.log" 2>&1\n')
    how = f"""\
Nightly `cdm rescan --checksum` at {hour:02d}:{minute:02d} as a cron job. To install:
  (crontab -l 2>/dev/null; cdm guide --schedule) | crontab -
To remove, delete the line with `crontab -e`."""
    return job, how
