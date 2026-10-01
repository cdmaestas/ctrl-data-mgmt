# ctrl-data-mgmt

A local file metadata catalog. Index the directories you care about, then ask
where things went, what's eating your disk, and what you're storing twice.

```console
$ cdm scan ~/work --checksum
/Users/you/work: 48210 files, 3115 dirs, 12 links in 41.2s (1,246 entries/s)
  hashed 48210 (3.1G read, 76.4M/s), reused 0 unchanged
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

## Getting started

`cdm guide` shows where you are and what to do next. Each step is checked
against the index itself, so it is done because the index shows it, not
because you said so:

```console
$ cdm guide
Getting started with cdm: 2 of 3 done

  [x] 1  Index a folder  -- /Users/you/work
  [x] 2  Hash it for duplicates  -- 100% of files hashed
  [>] 3  See what's worth doing  -- 1 safe and 4 to review, about 12.4G
         cdm ranks caches, stale build output, old installers, large git
         histories, model files and duplicates by size, with a risk and the
         command for each. It never runs anything.
         $ cdm suggest

  [~] 4  Clean up, then rescan
  [ ] 5  Keep it current  -- 1 root(s) not scanned in 7+ days, oldest 12 days
  [?] 6  Ask questions with an AI client  (optional)
```

The steps, in order:

1. **Index a folder**: `cdm scan ~/work --checksum`. Nothing is indexed until
   you name it; your home directory is a fine root, just never a default.
2. **Hash it**: `--checksum` on the first scan, or `cdm rescan --checksum`
   later. Unhashed files can't show up as duplicates.
3. **See what's worth doing**: `cdm suggest` (see below). Start with `safe`.
4. **Clean up, then rescan**: run the commands you choose, then
   `cdm rescan --checksum` so the index drops what's gone.
5. **Keep it current**: `cdm guide --schedule` prints a nightly rescan job for
   launchd (macOS) or cron to install yourself. On macOS:

   ```bash
   cdm guide --schedule > ~/Library/LaunchAgents/io.github.ctrl-data-mgmt.rescan.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.ctrl-data-mgmt.rescan.plist
   ```

6. **Optionally, ask questions with an AI client**: see
   [the MCP section](#ask-questions-with-an-ai-client-mcp).

`cdm guide --all` explains every step, and `--json` returns the same checklist
the MCP `guide` tool does.

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
| `cdm suggest` | ranked things worth doing, with the command for each |
| `cdm guide` | where you are and what to do next, step by step |
| `cdm stat PATH` | everything the index knows about one file, including last access where trusted |
| `cdm doctor` | index health, stale hashes, access-time coverage, roots that have gone away |

`du` answers from the index rather than the filesystem, so it returns instantly
on a tree that real `du` would spend minutes walking:

```bash
cdm du ~/work              # biggest subdirectories, one level down
cdm du ~/work --depth 2    # two levels
```

`find` filters compose, and all of them are optional:

```bash
cdm find --name '*.csv' --larger-than 10M --order mtime
cdm find --iname '*.pdf'            # case-insensitive; see the macOS note below
cdm find --modified-after 7d --type file --quiet | xargs wc -l
cdm find --accessed-before 180d --larger-than 1G   # big and not read in 6 months
cdm find --unopened --larger-than 100M             # downloaded or changed, never opened
cdm find --root ~/work --json
```

Sizes take binary units (`4096`, `1k`, `100M`, `2.5G`). Times take a relative
span (`7d`, `24h`) or a date (`2026-08-01`). `--quiet` prints bare paths for
piping; everything advisory goes to stderr, so pipelines stay clean.

### Nested roots

Roots may overlap: `~` and `~/src` can both be registered, say to rescan
`~/src` more often. Each file is indexed once and belongs to the **most
specific** root that contains it, whichever scan saw it last, so:

- a scan of either root reuses the hashes the other one computed;
- `cdm roots` counts each entry once, and a root's count leaves out what a root
  nested in it owns; the nested root is marked `(inside ~)`;
- `find --root ~` and the MCP tools scoped to `~` include `~/src`; `du` and
  `dupes` work by path and never double-count;
- `forget ~/src` hands its rows to `~`, which still covers them, rather than
  deleting them; `forget ~` drops only what `~` itself owns and leaves `~/src`
  whole.

The cost is walking the overlap twice on `cdm rescan`; `cdm scan` says so when
a new root overlaps an existing one.

## What to do about it: `cdm suggest`

`suggest` reads the index and says what is worth doing, biggest first, with the
command for each. It changes nothing: you decide and you run the commands.

```console
$ cdm suggest
 1  review    9.2G  Large git histories
                     Repacking usually shrinks history; a shallow re-clone shrinks
                     it most if you do not need the history locally.
                        7.8G  /Users/you/src/carbon/.git  (639 files, newest 2026-06-28)
                              $ git -C /Users/you/src/carbon gc --aggressive --prune=now
 2  safe      1.1G  Clear npm cache
                     Rebuilt automatically when needed.
                        1.1G  /Users/you/.npm/_cacache  (5,031 files, newest 2026-09-26)
                     $ npm cache clean --force
```

Each suggestion has a risk: **none** is index housekeeping (a stale scan, an
unhashed root), **safe** regenerates on its own (package caches), and **review**
means look first (build output, old installers, model files, large git
histories, duplicates, app caches). The rules are conservative on purpose:

- Age is the later of **last modified** and **last read**, where the
  filesystem keeps trustworthy access times (see below). A recent read may be
  Spotlight or a backup rather than you, so nothing judged by age alone is ever
  called safe.
- Dependencies and build output count only **inside a git checkout**, where
  "rebuild it" is actually true.
- A cache inside another listed cache is never counted twice, but savings can
  overlap between suggestions (a duplicate inside a cache appears in both).

`--older-than 30d` changes the staleness threshold, `--root` narrows to one root,
`--all` lists every path, and `--json` gives the same data the MCP tool returns.

## IBM Storage Scale

On Storage Scale, walking directories is the slow path; the policy engine reads
inode metadata directly. `cdm policy` prints a script for an administrator to
review and run as root — cdm itself never runs it or needs root:

```bash
cdm policy --device fs1 --fileset proj > cdm-list.sh   # read it, then as root:
sh cdm-list.sh                                          # writes fs1-proj.list.raw
cdm import --policy fs1-proj.list.raw                   # anywhere cdm runs
```

The script runs one `LIST` rule with `mmapplypolicy -I defer`, which only reads
metadata, and writes the listing (mode 0600) with a header and an end marker so
a truncated listing is never mistaken for a complete one. It covers one
fileset unless you pass `--whole-filesystem`, which warns that the index will
then hold every user's file names.

`cdm import --policy` refuses a listing that isn't provably complete, and
otherwise loads it in one transaction under a logical host,
`<filesystem>@<cluster>`, so listings from any node line up. From then on
`find`, `du`, `dupes`, `stat`, `suggest`, `guide` and the MCP tools cover your
own scans and every imported listing together.

Each import is a snapshot of what its listing covers: re-importing removes rows
for files that are gone. A fileset listing covers only that fileset — Storage
Scale leaves out other filesets even when they are linked under its junction —
so re-importing a fileset never removes a nested fileset's rows.

Each file's fileset and storage pool are kept: `cdm storage` totals the space
by pool and fileset, and `cdm find --pool data1` or `--fileset proj` narrows a
search. Over MCP, pools are named but filesets are reported by size rank unless
names are exposed, because fileset names are often a person's or a project's.

Access times come with the listing, under the same rules as on your own disks:
with Storage Scale's default `-S relatime` (or `no`), each file's last read and
whether it has been opened since it changed are recorded, so `find
--accessed-before`, `find --unopened` and `suggest` work on cluster data; with
`-S yes` (updates suppressed) they are recorded as unknown. `cdm doctor` shows
which applies.

A listing carries no file contents, so duplicates need one more step, on a
node that mounts the filesystem:

```bash
cdm hash --root /gpfs/fs1/proj     # then: cdm dupes
```

It reads only files whose size matches another indexed file's — a file of a
unique size can't be a duplicate — and keeps valid hashes, so a rerun resumes.
Its own reads don't count as use: the next import keeps the earlier last-read
time.

## Last access time

cdm records when each file was last read — but only where that date means
something, and never counting its own reads:

- **Only where reads update it.** A `noatime` or read-only mount never does.
  And measured on macOS APFS, a read moves the access time only on the *first*
  read after a file changes, so a model you load daily keeps the date of its
  first load. cdm measures this with a scratch file in its own data directory
  and, where the answer isn't "every read counts, within a day" (Linux
  `relatime` or better), records the access time as **unknown** rather than a
  misleading date. `cdm doctor` shows how many files have a trusted one.
- **Never polluted by cdm.** Hashing opens files with `O_NOATIME` on Linux.
  Elsewhere, cdm notes the access time its own read left behind and keeps the
  earlier one until someone else reads the file; `dupes --verify` does the
  same. Reads by scans from before this was recorded are treated as unknown.
- **Files only.** Listing a directory updates its access time, and cdm lists
  every directory it scans.

Where it is recorded, `stat` shows it, `find --accessed-before/--accessed-after`
filters on it (an unknown never matches), the MCP `age_histogram` takes
`by='atime'`, and `suggest` treats something read recently as in use. See
[ADR 0005](docs/adr/0005-access-time-is-recorded-only-where-it-means-last-read.md).

### Never opened since it changed

Even where last read isn't available — macOS included — the first read after a
change still moves the access time, so cdm can tell a file **not opened since
it was downloaded or last written**: an installer never run, a model never
loaded. `find --unopened` lists them, `stat` shows it, and `suggest` marks
installers and model stores "never opened".

It always comes with a date, because cdm's own first read uses the evidence up
on macOS: "not opened between its last change and *this date*". On Linux, where
cdm reads without touching access times, the date keeps up with each scan. For
an index built before this existed, cdm recovers the answer on macOS from when
its earlier scans read each file. See
[ADR 0006](docs/adr/0006-never-opened-is-recorded-with-the-date-it-was-last-true.md).

To check a filesystem cdm can't measure from your laptop — a Storage Scale or
NFS mount, say — run a scan with `CDM_DATA_DIR` pointed at a directory on it,
then `cdm doctor`: the probe measures the filesystem that holds the data
directory.

## Hashing: two kinds, never confused

Scanning records metadata only. Hashes are opt-in, and there are two, because
there are two different questions.

**`--checksum` (partial)** answers *are these probably the same file*. It reads
the first and last 64 KB (a file of 128 KB or less is read whole) and mixes in
the exact byte count. On a large tree
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

## Absence is observed, never assumed

A scan decides a file is gone by not having seen it — and that inference is only
drawn for directories it could actually read. If a directory can't be opened
(permissions, an unmounted share, macOS privacy controls), nothing beneath it is
removed and its existing entries are kept.

If the *root* can't be opened, the scan indexes nothing, removes nothing, leaves
the root's last-scan time alone, and exits non-zero — reported as a failure, not
as an empty directory, because those look identical from outside and only one is
true.

This matters more than it sounds. Without it, running `cdm rescan` while a
network share happens to be unmounted deletes every row for that root and
cheerfully reports the files as "no longer on disk."

## Ask questions with an AI client (MCP)

`cdm mcp` serves the index to an MCP client such as Claude Code or Claude
Desktop, so you can ask *"what's eating the disk under ~/work, and how much of it
hasn't been touched in a year?"* and get an answer from the index.

It needs Python 3.10+ and the optional SDK, which the core deliberately doesn't
carry:

```bash
pipx inject ctrl-data-mgmt mcp
claude mcp add --scope user cdm -- cdm mcp
```

For Claude Desktop, add this to `~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS. Use the absolute path from `which cdm`, because Desktop doesn't
inherit your shell's `PATH`:

```json
{"mcpServers": {"cdm": {"command": "/Users/you/.local/bin/cdm", "args": ["mcp"]}}}
```

**Names are not exposed by default.** With a cloud-hosted model, everything a
tool returns goes to its provider, and filenames are often sensitive on their
own: codenames, people's names, case numbers baked into paths. So out of the box
the server answers from the *shape* of your data, not its names:

| tool | answers |
|---|---|
| `summary` | per-root files, bytes, and days since last scan |
| `size_histogram` | is the space in a few huge files or many small ones |
| `age_histogram` | how much of this is cold, by last modified or (`by='atime'`) last read |
| `extensions` | what kind of data is taking the space |
| `duplicates_summary` | how much looks duplicated, how much is confirmed |
| `suggest` | what is worth doing, ranked, with risk and command (no paths) |
| `guide` | the getting-started checklist, with the next step |
| `storage` | Storage Scale space by pool (named) and fileset (ranked, not named) |

To make it easy to start, the server also offers **prompts** — *Getting
started* (a step-by-step walk through `guide`), *What's using my disk?*, *What can I clean up?*, *How much is duplicated?*, *What changed
recently?*, *Is the index up to date?* — which Claude Code lists as slash
commands. Every tool result carries `next_steps` pointing at the next useful
tool, and the model is told to show `suggest`'s commands, never run them.

Start it with `--expose-names` to add `find`, `du`, `dupes` and `stat`, which
return paths, and to let `suggest` list the paths behind each suggestion.
Without the flag those tools aren't filtered, they're **not registered at all**,
so a client can't call them or even learn they exist.

The server is read-only in the strong sense: it opens the index with SQLite's
read-only mode, so a write is refused by SQLite itself. It never walks
directories or reads file contents. Keep the index fresh with `cdm rescan`;
`summary` tells the model how stale each root is.

## On macOS

Fully supported — CI runs the suite on macOS, and the performance numbers above
were measured on an arm64 Mac. Two platform specifics:

**Privacy controls.** Parts of your home directory — `~/Library/Mail`,
`~/Library/Messages`, Safari data, the Photos library — can't be read without
granting Full Disk Access to your terminal. Without it those paths are reported
as unreadable and skipped, so the index is simply incomplete for them rather
than wrong about them.

**Case-insensitive filesystems.** APFS treats `Report.TXT` and `report.txt` as
the same name; SQL `GLOB` doesn't. So `--name '*.txt'` won't match `Report.TXT`.
Use `--iname` when you want the filesystem's own notion of sameness.

## Where the data lives

One SQLite file at `$XDG_DATA_HOME/ctrl-data-mgmt/index.db` (`~/.local/share/...`),
mode `0600`, in a `0700` directory — an index of every filename you own is more
revealing than most file contents. `CDM_DATA_DIR` or `CDM_INDEX` override it.

That applies to SQLite's `-wal` and `-shm` sidecars too: they hold the same
filename data, SQLite creates them itself, and they inherit the database's mode
only if it is already correct when the write-ahead log is enabled. If a mode
can't be set — some filesystems don't support `chmod` — you get a warning naming
the file rather than silence, and `cdm doctor` exits non-zero so a script can
gate on it.

Rows carry a `host` column, populated with the local hostname. v1 only ever
scans locally; the column is there so a later fan-out across machines is a merge
of per-host indexes rather than a migration of an index you've come to rely on.

## Speed and concurrency

A stat-only pass runs at roughly 30k entries/second on a warm local disk — about
3 seconds for 99,000 entries. A rescan of a quiet tree costs one `stat` per file
and reuses every hash.

On a terminal, a scan shows a live line with entries/second, hash throughput and
elapsed time. When stderr is a log or the scan runs in the background, add
`--progress` (every 10s, or `--progress SECS`) to get the same figures as
timestamped lines; the final summary always includes the rate.

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

Note that on a parallel filesystem — Storage Scale, Lustre — a POSIX walk is
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

- **No built-in natural language.** Plain-language questions come through an
  MCP client instead (see below), which compiles them into the same query layer
  the CLI uses. Nothing ships a model.
- **No remote scans.** Multi-host fan-out is the reason `host` exists, not
  something v1 does.

What comes next, and in what order, is in [docs/roadmap.md](docs/roadmap.md).

## Documentation

- **`man/cdm.1`** — the reference: every verb, every flag, exit statuses,
  environment variables. Read it from a checkout with `man ./man/cdm.1`. A pipx
  install links it into `~/.local/share/man`, so `man cdm` just works; a plain
  virtualenv install leaves it inside the venv.
- **[docs/roadmap.md](docs/roadmap.md)** — the phases after this one and the
  backlog: Storage Scale at scale, a UI, then packaging for Linux, macOS and
  Windows.
- **[docs/multi-host.md](docs/multi-host.md)** — design note on scanning many
  hosts with `pdsh`, and why the index must never live on the shared filesystem.
  Scheduled for phase 2 (see the roadmap); not implemented yet.
- **[docs/adr/](docs/adr/)** — decision records: why names are opt-in over MCP
  (0002), why suggestions come from one advisory engine (0003), and why the
  getting-started guide takes its status from the index (0004), when an access
  time is trusted (0005), how "never opened" is dated (0006), and why pool
  names reach the AI by default but fileset names don't (0007).
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — setup, and the list of choices that
  are deliberate rather than accidental.
- **[docs/releasing.md](docs/releasing.md)** — how a release happens, and the
  one-time PyPI Trusted Publishing setup it depends on.

## Development

```bash
pip install -e ".[dev]"
git config core.hooksPath .githooks   # once per clone
pytest
ruff check .
mandoc -Tlint man/cdm.1
```

CI checks that the man page and the CLI agree on the verb list, so a new
command cannot ship undocumented.

## Licence

Apache-2.0.
