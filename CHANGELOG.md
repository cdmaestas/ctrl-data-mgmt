# Changelog

Notable changes to `ctrl-data-mgmt` (`cdm`). The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Index schema changes are called out
under **Upgrading**, because they change what an existing index contains.

## [Unreleased]

Nothing has been published to PyPI yet; this is everything since the first
commit. It becomes `0.1.0` when that is tagged.

### Upgrading

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
- Man page `cdm(1)`, contributing guide, and version-controlled git hooks.
- Release workflow: Trusted Publishing, TestPyPI and a smoke test before PyPI.

### Fixed

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
