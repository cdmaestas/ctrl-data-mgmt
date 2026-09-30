# Changelog

Notable changes to `ctrl-data-mgmt` (`cdm`). The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Index schema changes are called out
under **Upgrading**, because they change what an existing index contains.

## [Unreleased]

Nothing has been published to PyPI yet; this is everything since the first
commit. It becomes `0.1.0` when that is tagged.

### Upgrading

- **Index schema 5** adds "not opened since it last changed" (`unopened_until`).
  Added in place on first use; on macOS the next scan also recovers it for files
  earlier cdm scans hashed. Restart a running `cdm mcp` afterwards.
- **Index schema 4** adds last access time. The first read-write command adds
  the columns; nothing is lost. Access times fill in on the next scan, and only
  where they are trustworthy (see *Added*). Restart a running `cdm mcp` after
  upgrading.
- **Index schema 3.** The first read-write command (e.g. `cdm doctor`) upgrades
  an existing index. The upgrade clears partial hashes for files between 64 KiB
  and 128 KiB, which earlier builds computed wrongly (see *Fixed*); run
  `cdm rescan --checksum` to recompute them. Until then those files are absent
  from `cdm dupes`. Read-only front-ends such as `cdm mcp` refuse an index on
  an older schema and say so; a running MCP server must be restarted after the
  upgrade.

### Added

- `cdm scan` / `rescan`: record file metadata for explicit roots into a local
  SQLite index. Symlinks are recorded, never followed. Credential paths are
  excluded by default, and every skip is counted and reported.
- Two hash kinds, stored per row and never pooled: `--checksum` (partial: both
  ends plus the byte count, for dedupe) and `--full-checksum` (every byte, for
  integrity). Unchanged files reuse their hash on rescan.
- `cdm find`, `du`, `dupes` (with `--verify` to confirm partial-hash groups in
  full), `stat`, `roots`, `forget` and `doctor`.
- `--iname`, for case-insensitive filesystems such as APFS.
- Threaded directory walking for latency-bound (remote) filesystems, with the
  thread count chosen by measuring stat latency; `-j` overrides it.
- Resumable scans: each directory is checkpointed in the same transaction as
  its rows, so an interrupted scan resumes where it stopped.
- Nested roots: `~` and `~/src` may both be registered. Each file belongs to
  the most specific root, hashes are shared between them, and `forget` of a
  nested root hands its rows to the enclosing root.
- Scan throughput in the summary (entries/s, bytes hashed and rate), a live
  progress line on a terminal, and `--progress [SECS]` for timestamped lines
  in logs and background runs.
- `cdm mcp`: a read-only MCP server so an AI client can answer questions about
  the index. By default it exposes only aggregate tools that return no names;
  `--expose-names` adds `find`, `du`, `dupes` and `stat`. Requires the `mcp`
  extra (Python 3.10+); the core stays dependency-free on 3.9.
- `cdm suggest` and the MCP `suggest` tool: ranked, advisory suggestions
  (caches, stale dependencies and build output in git checkouts, large git
  histories, old installers, model files, duplicates, index housekeeping), each
  with its size, reason, risk and command. Nothing is ever run. See
  `docs/adr/0003`.
- MCP prompts (`disk-usage`, `cleanup`, `duplicates`, `recent-changes`,
  `index-health`) that clients list as ready-made questions, and a `next_steps`
  field on every tool result pointing at the next useful tool.
- `cdm guide`, the MCP `guide` tool and a `getting-started` prompt: a fixed
  checklist from first scan to a well-kept index, with the next step
  explained. Each step's status is read from the index, never assumed. See
  `docs/adr/0004`. `cdm guide --schedule` prints a nightly rescan job (launchd
  or cron) to install yourself.
- README: a Getting started section.
- `cdm policy`: prints a script that lists an IBM Storage Scale fileset (or,
  with `--whole-filesystem`, a whole filesystem) with `mmapplypolicy -I defer`,
  for an administrator to run as root; cdm never runs it. The listing carries
  a header (cluster, scope, the `-S` atime setting, field order) and an end
  marker with the row count, is written mode 0600, and must be a `.raw` file.
  Names are validated, not escaped. Import follows in 0.2.0.
- Guards against committing raw cluster data, ahead of Storage Scale support:
  `.gitignore` entries for raw listings and export dumps, a required header on
  test fixtures, and `scripts/check_raw_data.py`, which the pre-commit hook runs
  on staged content and CI runs on every tracked file. It also catches a raw
  policy listing saved under an innocent name.
- Last access time for files, recorded only where it means "last read": cdm
  measures its own data directory's filesystem and reads mount options
  elsewhere, and records an untrustworthy one (noatime, read-only, macOS APFS's
  first-read-only updates) as unknown. cdm's own reads never count
  (`O_NOATIME` on Linux, otherwise its reads are remembered and discounted).
  `stat` shows it, `find --accessed-before/--accessed-after` and
  `--order atime` use it, MCP `age_histogram` takes `by='atime'`, `suggest`
  judges age by the later of modified and last read, and `doctor` reports
  coverage. See `docs/adr/0005`.
- "Never opened since it changed", recorded wherever the first read after a
  change moves the access time (macOS APFS included) and always with the date
  it was last known true, since cdm's own first read uses the evidence up on
  macOS. `find --unopened`, `stat`, MCP `find(unopened=true)`, and `suggest`
  marks installers and model stores "never opened (as of …)". The access-time
  probe now tells three filesystem behaviours apart. See `docs/adr/0006`.
- Man page `cdm(1)`, contributing guide, and version-controlled git hooks.
- Release workflow: Trusted Publishing, TestPyPI and a smoke test before PyPI.

### Fixed

- `cdm guide --schedule` built the launchd plist and cron line by pasting the
  executable path in: a path containing `&` or `<` made a plist launchd
  refuses, and in the cron line `&`, `;` or `%` split or broke the command. The
  plist is now generated with `plistlib`, the cron command shell-quoted with
  `%` escaped. When `cdm` is not on `PATH` the job now runs
  `python -m cdm`; it used to fall back to `sys.argv[0]`, which under
  `python -m cdm` pointed at `__main__.py` and failed every night.
- A failed access-time measurement is no longer silent: the scan says it fell
  back to mount options, and `doctor` gives the reason. Failing to remove the
  probe's scratch file counts as a failure instead of leaving it unmentioned.
- If cdm cannot read a file's access time back after its own read, the file's
  access time and "never opened" are recorded as unknown. Previously the error
  was ignored and the next scan counted cdm's read as a use. `dupes --verify`
  reports the same case instead of skipping it.
- The man page said scans are single-threaded; walking has used threads on
  remote filesystems since the start. Only hashing is.
- A rescan now removes everything beneath a directory that was deleted, not
  just the directory itself. Previously the deeper rows stayed in the index
  forever, so a cleared cache kept showing up in `du`, `suggest` and MCP.
- A rescan without `--checksum` keeps hashes that still match instead of
  erasing them, so `cdm rescan` no longer empties `cdm dupes`.
- Partial hashes of files between 64 KiB and 128 KiB covered only the first
  64 KiB, so same-sized files differing after that were reported as duplicates.
  Files of up to 128 KiB are now hashed whole.
- A rescan no longer deletes rows beneath a directory it could not read (an
  unmounted share, a permissions change, macOS privacy controls). Previously
  they were pruned as "no longer on disk" with exit status 0. An unreadable
  root is now an error.
- Scanning a tree that contains the index (e.g. `$HOME`) no longer opens the
  index's own files for hashing, which could let a concurrent `cdm` process
  delete the write-ahead log mid-scan and crash the scanner.
- `dupes --verify` reports files it could not read instead of dropping them,
  and exits non-zero.
- `doctor` exits 1 when it finds a problem.

### Security

- The index and its `-wal` / `-shm` sidecars are created `0600` and verified
  after creation; a filesystem that cannot enforce the mode is reported loudly
  rather than ignored.

[Unreleased]: https://github.com/cdmaestas/ctrl-data-mgmt/commits/main
