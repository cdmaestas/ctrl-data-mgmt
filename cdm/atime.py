"""Last access time: recorded only where it means something. See docs/adr/0005.

Access time is the one date that says "is anyone still using this", and the
easiest to get wrong three ways:

* A filesystem mounted `noatime` (common on servers) or read-only never updates
  it, so the value is a fossil. Worse, some update it only on the FIRST read
  after a change: measured on macOS APFS, a read moves an atime older than the
  mtime and leaves one newer than it alone, however old. There, a model loaded
  daily keeps the date of its first load. So trust means "a read of a file
  last read more than a day ago moves its atime" -- Linux `relatime`
  semantics or better -- and an untrusted atime is stored as NULL, never as a
  date. It is measured where cdm may write (its own data directory) and read
  from mount options elsewhere.

  A FIRST filesystem still answers a narrower question: has the file been
  read at all since it last changed? That is recorded separately, as
  `unopened_until`, dated because cdm's own first read uses the answer up.
  See docs/adr/0006.
* cdm's own reads update it. Hashing opens every new file, so without care the
  first `--checksum` scan would make everything look read today. See scan.py
  for how its own reads are recognised and discounted, and hashing.py for
  O_NOATIME on Linux.
* Other readers update it too -- Spotlight, backup tools, virus scanners. So a
  recent atime means "read by something", not "used by you". It is evidence of
  disuse when old, never proof of use when new.

Directories are never given one: listing a directory updates its atime, and
cdm lists every directory it scans.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Options that mean the atime on that filesystem never moves.
_FROZEN = ("noatime", "read-only", "ro")

DAY = 86400
# What a file's atime means on a filesystem:
LAST = "last"     # every read moves it (to within a day): it is the last read
FIRST = "first"   # only the first read after a change moves it (macOS APFS)
NONE = "none"     # reads never move it (noatime, read-only), or it is unknown


def _mount_table() -> list[tuple[str, set[str]]]:
    """(mount point, options) for each mounted filesystem, longest path first."""
    table: list[tuple[str, set[str]]] = []
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/mounts", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 4:
                        # /proc/mounts escapes spaces in paths as \040.
                        point = parts[1].replace("\\040", " ")
                        table.append((point, set(parts[3].split(","))))
        elif sys.platform == "darwin":
            out = subprocess.run(["/sbin/mount"], capture_output=True, text=True,
                                 timeout=10, check=False).stdout
            for line in out.splitlines():
                # "/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled)"
                if " on " not in line or "(" not in line:
                    continue
                rest = line.split(" on ", 1)[1]
                point, _, opts = rest.rpartition(" (")
                table.append((point, {o.strip() for o in opts.rstrip(")").split(",")}))
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(table, key=lambda t: len(t[0]), reverse=True)


def _verdict(point: str, opts: set[str], platform: str) -> tuple[bool, str]:
    frozen = sorted(opts.intersection(_FROZEN))
    if frozen:
        return False, f"{point} is mounted {', '.join(frozen)}"
    if platform == "darwin":
        # Mount options cannot show APFS's first-read-only rule; only a
        # measurement can, and this device was not measured.
        return False, f"{point}: unmeasured, and macOS may update access times " \
                      f"only on the first read after a change"
    return True, f"{point} updates access times"


class ProbeFailed(Exception):
    """The measurement could not be made; the reason is the message."""


def probe(directory: Path) -> tuple[int, str]:
    """(device, mode) measured with a scratch file in `directory`.

    Two reads tell the three modes apart. A read that moves a day-old atime
    NEWER than the mtime means every read counts, to within a day: LAST. If not,
    a read that moves an atime OLDER than the mtime means only the first read
    after a change counts: FIRST (measured on macOS APFS). If neither moves it:
    NONE. `directory` must be somewhere cdm may write.

    Raises ProbeFailed rather than returning a quiet default: a failed
    measurement falls back to mount options, and whoever shows the verdict
    must be able to say it was not measured, and why. Failing to remove the
    scratch file is a failure too -- it would otherwise sit in the data
    directory unmentioned.
    """
    try:
        # 0700, like the data directory itself; normally it already exists.
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".cdm-atime-probe-", dir=directory)
    except OSError as exc:
        raise ProbeFailed(f"could not create a scratch file in {directory}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b"probe")
        now = time.time()

        def moves(atime: float, mtime: float) -> bool:
            os.utime(name, (atime, mtime))
            before = os.stat(name).st_atime
            with open(name, "rb") as f:
                f.read()
            return os.stat(name).st_atime != before

        dev = os.stat(name).st_dev
        if moves(now - 2 * DAY, now - 3 * DAY):
            result = dev, LAST
        elif moves(now - 3 * DAY, now - 2 * DAY):
            result = dev, FIRST
        else:
            result = dev, NONE
    except OSError as exc:
        _remove(name)
        raise ProbeFailed(f"measuring in {directory} failed: {exc}") from exc
    try:
        os.unlink(name)
    except OSError as exc:
        raise ProbeFailed(f"measured, but could not remove the scratch file {name}: "
                          f"{exc}") from exc
    return result


def measure(directory: Path) -> tuple[tuple[int, str] | None, str | None]:
    """probe(), for callers that fall back: (result, None) or (None, why not)."""
    try:
        return probe(directory), None
    except ProbeFailed as exc:
        return None, str(exc)


def _remove(name: str) -> None:
    try:
        os.unlink(name)
    except OSError:
        pass   # already failing; the ProbeFailed raised by the caller says so


_MEASURED = {
    LAST: "measured: every read moves the access time (to within a day)",
    FIRST: "measured: only the first read after a change moves the access time",
    NONE: "measured: reads never move the access time",
}


def _verdict(point: str, opts: set[str], platform: str) -> tuple[str, str]:
    frozen = sorted(opts.intersection(_FROZEN))
    if frozen:
        return NONE, f"{point} is mounted {', '.join(frozen)}"
    if platform == "darwin":
        # Mount options cannot show APFS's first-read-only rule; only a
        # measurement can, and this device was not measured.
        return NONE, (f"{point}: not measured, and macOS may update access "
                      f"times only on the first read after a change")
    return LAST, f"{point} updates access times"


class Trust:
    """What a file's atime means, decided once per device: LAST, FIRST or NONE."""

    def __init__(self, table: list[tuple[str, set[str]]] | None = None,
                 measured: tuple[int, str] | None = None,
                 platform: str | None = None, unmeasured: str | None = None):
        self._table = _mount_table() if table is None else table
        self._platform = platform or sys.platform
        # Why the measurement was not made, if it was attempted and failed;
        # appended to every verdict that falls back to mount options.
        self._unmeasured = unmeasured
        self._by_dev: dict[int, tuple[str, str]] = {}
        if measured is not None:
            dev, mode = measured
            self._by_dev[dev] = (mode, _MEASURED[mode])

    def check(self, path: str, dev: int) -> tuple[str, str]:
        """(mode, why) for the filesystem holding `path`."""
        cached = self._by_dev.get(dev)
        if cached is None:
            mode, why = self._decide(path, dev)
            if self._unmeasured:
                why += f"; not measured: {self._unmeasured}"
            cached = self._by_dev[dev] = (mode, why)
        return cached

    def _decide(self, path: str, dev: int) -> tuple[str, str]:
        # By device first. Paths mislead: on macOS /Users is a firmlink into
        # /System/Volumes/Data, so by path it sits under the read-only system
        # volume at / while its bytes live on a writable one.
        for point, opts in self._table:
            try:
                if os.stat(point).st_dev == dev:
                    return _verdict(point, opts, self._platform)
            except OSError:
                continue
        for point, opts in self._table:
            if path == point or path.startswith(point.rstrip("/") + "/"):
                return _verdict(point, opts, self._platform)
        # No mount table (an unsupported platform, or it could not be read):
        # an unverifiable atime is not recorded rather than guessed at.
        return NONE, "could not read the mount table to check"


def open_quietly(path: Path | str):
    """Open for reading without updating atime where the OS allows it.

    O_NOATIME exists on Linux and only works for files you own; anywhere else,
    or on EPERM, this is a plain open and scan.py discounts the read instead.
    """
    flag = getattr(os, "O_NOATIME", 0)
    if flag:
        try:
            return os.fdopen(os.open(path, os.O_RDONLY | flag), "rb")
        except PermissionError:
            pass
    return open(path, "rb")
