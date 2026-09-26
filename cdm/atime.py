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


def probe(directory: Path) -> tuple[int, bool] | None:
    """(device, whether a read moves a day-old atime newer than the mtime).

    The one test that tells "last read" semantics from "first read after a
    change" apart. Uses a scratch file in `directory`, which must be somewhere
    cdm may write; returns None if that is not possible.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".cdm-atime-probe-", dir=directory)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b"probe")
        now = time.time()
        os.utime(name, (now - 2 * 86400, now - 3 * 86400))   # atime newer than mtime
        before = os.stat(name).st_atime
        with open(name, "rb") as f:
            f.read()
        st = os.stat(name)
        return st.st_dev, st.st_atime != before
    except OSError:
        return None
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


class Trust:
    """Whether a file's atime can be believed, decided once per device."""

    def __init__(self, table: list[tuple[str, set[str]]] | None = None,
                 measured: tuple[int, bool] | None = None,
                 platform: str | None = None):
        self._table = _mount_table() if table is None else table
        self._platform = platform or sys.platform
        self._by_dev: dict[int, tuple[bool, str]] = {}
        if measured is not None:
            dev, moves = measured
            self._by_dev[dev] = (moves, "measured: a read moves a day-old access time"
                                 if moves else "measured: a read only moves the access "
                                 "time on the first read after a change")

    def check(self, path: str, dev: int) -> tuple[bool, str]:
        cached = self._by_dev.get(dev)
        if cached is None:
            cached = self._by_dev[dev] = self._decide(path, dev)
        return cached

    def _decide(self, path: str, dev: int) -> tuple[bool, str]:
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
        return False, "could not read the mount table to check"


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
