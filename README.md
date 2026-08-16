# ctrl-data-mgmt

A local file metadata catalog. Index the directories you care about, then ask
where things went, what's eating your disk, and what you're storing twice.

```console
$ cdm scan ~/work --checksum
/Users/you/work: 48210 files, 3115 dirs, 12 links in 41.2s
  hashed 48210, reused 0 unchanged
  skipped 3 credential path(s) (--no-skip-credentials to include)

$ cdm find --larger-than 500M --modified-before 90d
  2.1G  2025-11-03 14:22  /Users/you/work/archive/2025-dump.tar
  812M  2026-01-18 09:40  /Users/you/work/models/checkpoint-4000.bin

$ cdm dupes --min-size 100M --verify
1.4G x2  (verified)
    /Users/you/work/raw/scan-A.tiff
    /Users/you/work/backup/scan-A.tiff
reclaimable: 1.4G
```

Nothing leaves the machine. Pure Python standard library — no dependencies, no
services, no daemon.

## Install

```bash
pipx install ctrl-data-mgmt
```

`pip install ctrl-data-mgmt` works too, inside a virtualenv.

## Use

Roots are explicit. There is no default `$HOME` crawl, ever — you say what to
index:

```bash
cdm scan ~/work ~/datasets
cdm rescan                    # all known roots, reusing unchanged hashes
cdm roots                     # what's watched, and when it was last scanned
```

| command | what it does |
|---|---|
| `cdm scan PATH...` | add a root and index it |
| `cdm rescan [PATH...]` | re-index; unchanged files cost one stat each |
| `cdm forget PATH` | drop a root and its rows; touches nothing on disk |
| `cdm find [filters]` | query the index |
| `cdm du [PATH]` | disk usage by subdirectory, answered from the index |
| `cdm dupes` | files that look identical |
| `cdm stat PATH` | everything the index knows about one file |
| `cdm doctor` | index health, stale hashes, roots that have gone away |

`du` answers from the index rather than the filesystem, so it returns instantly
on a tree that real `du` would spend minutes walking:

```bash
cdm du ~/work              # biggest subdirectories, one level down
cdm du ~/work --depth 2    # two levels
```

`find` filters compose, and all of them are optional:

```bash
cdm find --name '*.csv' --larger-than 10M --order mtime
cdm find --modified-after 7d --type file --quiet | xargs wc -l
cdm find --root ~/work --json
```

Sizes take binary units (`4096`, `1k`, `100M`, `2.5G`). Times take a relative
span (`7d`, `24h`) or a date (`2026-08-01`). `--quiet` prints bare paths for
piping; everything advisory goes to stderr, so pipelines stay clean.

## Hashing: two kinds, never confused

Scanning records metadata only. Hashes are opt-in, and there are two, because
there are two different questions.

**`--checksum` (partial)** answers *are these probably the same file*. It reads
the first and last 64 KB and mixes in the exact byte count. On a large tree
that's the difference between minutes and a weekend, and for finding duplicates
it's very nearly as good as reading everything.

**`--full-checksum`** answers *is this byte-for-byte what I recorded*. No
shortcut exists, and none is offered.

Which kind produced a row is stored in that row, so a partial digest can never
be pooled with a full one. `cdm dupes --verify` re-reads partial-hash candidates
in full and reports only the groups that survive — the partial hash proposes,
the full hash confirms.

Each hash also records the size and mtime it was computed against, so a file
that changed after it was hashed shows as `STALE` rather than quietly reporting
a hash that is no longer true.

## What it won't index

- **Credential paths**, by default: `.ssh`, `.gnupg`, `.aws`, `.kube`,
  keychains, browser profiles, `*.pem`, `*.key`. This tool tells you what you
  own; it is not for building a convenient index of where your keys live.
  `--no-skip-credentials` if you really want them.
- **Anything through a symlink.** Links are recorded as links; what they point
  at is somebody else's root. One link into `/proc` would otherwise turn a scan
  into a hang.
- **`.gitignore` is deliberately not honoured.** It describes what git should
  ignore, not what exists on your disk — and "what's eating my disk" is one of
  the questions this is for.

Every skip is counted and printed. An index that silently omits things is worse
than one that refuses out loud.

## Where the data lives

One SQLite file at `$XDG_DATA_HOME/ctrl-data-mgmt/index.db` (`~/.local/share/...`),
mode `0600`, in a `0700` directory — an index of every filename you own is more
revealing than most file contents. `CDM_DATA_DIR` or `CDM_INDEX` override it.

Rows carry a `host` column, populated with the local hostname. v1 only ever
scans locally; the column is there so a later fan-out across machines is a merge
of per-host indexes rather than a migration of an index you've come to rely on.

## Speed and concurrency

A stat-only pass runs at roughly 30k entries/second on a warm local disk — about
3 seconds for 99,000 entries. A rescan of a quiet tree costs one `stat` per file
and reuses every hash.

Remote filesystems are a different problem: at a realistic 0.5ms metadata round
trip a single thread manages only ~1,600 entries/second, so the walk is
**latency-bound**, and threads fix latency. Measured against simulated latency:

| per-op latency | 8 threads | 32 threads |
|---|---|---|
| 0.1ms (fast) | 7.1× | 14.9× |
| 0.5ms (typical remote) | 7.5× | 26.9× |
| 2ms (loaded server) | 7.9× | 30.4× |

On a warm **local** disk that same threading is a **4× slowdown** — no latency
to hide, so concurrency only adds contention. So the thread count is measured
rather than assumed: `cdm` times a sample of `stat` calls at startup and picks 1
locally, up to 8 remotely, printing what it found. `-j N` overrides.

The remote default stays conservative on purpose. On a shared cluster you are
one of many users of the metadata servers, and being noticed by all of them is
worse than a slower scan. Hashing is always single-threaded: it's bandwidth-bound,
so concurrency buys little and saturating shared storage costs a lot.

Scans print a running count to stderr, but only when stderr is a terminal.

Note that on a parallel filesystem — Spectrum Scale, Lustre — a POSIX walk is
the slow path by design; the native policy engine reads metadata far faster
than `scandir` can at any thread count. See
[docs/multi-host.md](docs/multi-host.md).

## Interrupted scans resume

Each directory is checkpointed in the same database transaction as the entries
it contains, so a scan killed at any point leaves a consistent index — nothing
is marked done that wasn't written. Re-running `cdm scan` on the same root picks
up where it stopped:

```console
$ cdm scan /scratch/project      # killed part-way through
$ cdm scan /scratch/project
  resumed from a checkpoint: 4400 directories already done
```

Verified against a killed scan of 99,000 entries: the resumed index is identical
to a clean one, path for path, and finishes faster than starting over.
`--restart` discards the checkpoint. A checkpoint is only resumed when the hash
kind matches, since resuming a stat-only scan with `--checksum` would leave half
a tree hashed with nothing recording which half.

## Not in this version

- **No natural language.** The flag-driven CLI comes first on purpose: it's the
  substrate an NL layer would compile into, and using it daily is what produces
  the log of real questions needed to evaluate one honestly.
- **No remote scans.** Multi-host fan-out is the reason `host` exists, not
  something v1 does.

## Documentation

- **`man/cdm.1`** — the reference: every verb, every flag, exit statuses,
  environment variables. Read it from a checkout with `man ./man/cdm.1`. It
  installs to `share/man/man1`, though a pipx or venv install puts that inside
  the venv rather than on your `MANPATH`.
- **[docs/multi-host.md](docs/multi-host.md)** — design note on scanning many
  hosts with `pdsh`, and why the index must never live on the shared filesystem.
  Not implemented; recorded so the decisions that keep it cheap survive.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — setup, and the list of choices that
  are deliberate rather than accidental.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
mandoc -Tlint man/cdm.1
```

CI checks that the man page and the CLI agree on the verb list, so a new
command cannot ship undocumented.

## Licence

Apache-2.0.
