"""Decide how many walker threads to use by measuring, not guessing.

Threading a metadata walk only helps when there is latency to hide. Measured
on this project:

    local SSD, warm cache   threading is a 4x LOSS -- syscalls return in
                            microseconds and all you add is contention
    0.5ms per operation     ~27x faster at 32 threads
    2ms per operation       ~30x faster at 32 threads

So the useful question is not "how many cores" but "how far away is this
filesystem", and that is answerable in about a tenth of a second by timing a
sample of stat calls.

The default stays deliberately conservative on remote filesystems. On a
customer's shared cluster you are a guest, and enough concurrent metadata
traffic to be noticed by other users is a worse outcome than a slower scan.
`--threads` exists for when you know what you are doing.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

SAMPLE = 200
LOCAL_THRESHOLD = 0.0001   # 0.1ms per stat: local disk or a warm cache
DEFAULT_REMOTE_THREADS = 8
MAX_AUTO_THREADS = 8


class ProbeResult:
    def __init__(self, median: float, sampled: int, threads: int):
        self.median = median
        self.sampled = sampled
        self.threads = threads

    @property
    def is_local(self) -> bool:
        return self.median < LOCAL_THRESHOLD

    def describe(self) -> str:
        where = "local" if self.is_local else "remote"
        return (f"probed {self.sampled} entries: {self.median * 1000:.3f}ms/stat "
                f"({where}) -> {self.threads} thread"
                f"{'s' if self.threads != 1 else ''}")


def probe(root: Path, sample: int = SAMPLE) -> ProbeResult:
    """Time a sample of stat calls under `root` and pick a thread count."""
    timings: list[float] = []
    stack = [Path(root)]
    seen_dirs = 0

    while stack and len(timings) < sample:
        directory = stack.pop()
        seen_dirs += 1
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if len(timings) >= sample:
                break
            start = time.perf_counter()
            try:
                entry.stat(follow_symlinks=False)
            except OSError:
                continue
            timings.append(time.perf_counter() - start)
            # Descend a little so the sample is not one unrepresentative
            # directory, but do not walk the whole tree to answer this.
            if seen_dirs < 8 and entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))

    if not timings:
        return ProbeResult(0.0, 0, 1)

    timings.sort()
    median = timings[len(timings) // 2]
    threads = 1 if median < LOCAL_THRESHOLD else min(DEFAULT_REMOTE_THREADS,
                                                     MAX_AUTO_THREADS)
    return ProbeResult(median, len(timings), threads)
