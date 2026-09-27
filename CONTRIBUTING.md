# Contributing

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

Enable the pre-commit hook once per clone:

```bash
git config core.hooksPath .githooks
```

It runs ruff, the tests, and a check that the man page still documents every
verb — about a second in total. `git commit --no-verify` bypasses it for a WIP
commit. Hooks live in `.githooks/` rather than `.git/hooks/` so they are version
controlled and everyone gets the same ones.

No runtime dependencies, and it stays that way. `sqlite3`, `hashlib` and
`os.scandir` are standard library; anything that would need a wheel needs an
argument first.

Python 3.9 is the floor, so no `match`, no `X | Y` at runtime (the
`from __future__ import annotations` at the top of each module is what makes the
annotations legal), and no `tomllib`.

## Tests

`pytest` runs in under a second. Keep it that way — a suite you don't run isn't
a suite. Everything goes through `tmp_path`; no test may touch the real index,
which is what the `CDM_DATA_DIR` fixture in `tests/test_cli.py` enforces.

A test should fail for the reason it claims. If you add one for an escaping or
boundary bug, check that it actually fails against the unfixed code before
trusting it.

## Things that are decisions, not accidents

Change these only deliberately, and update the man page when you do:

- **Roots are explicit.** No default `$HOME` crawl, ever.
- **Symlinks are recorded, never followed.**
- **Credential paths are excluded by default, and every skip is counted and
  printed.** Silent omission is the failure mode that makes an index untrustworthy.
- **`hash_kind` is stored per row.** A partial digest must never be poolable
  with a full one.
- **`hash_size` and `hash_mtime` record what a hash was computed against**, so
  staleness is detectable rather than silently wrong.
- **The index is 0600 in a 0700 directory.** CI asserts this independently of
  the unit tests.
- **In `db.connect`, the file's mode is fixed BEFORE `journal_mode=WAL` runs.**
  SQLite copies the database's permissions onto `-wal` and `-shm` when it
  creates them, so swapping those two lines silently leaks the same filename
  data at 0644. `tests/test_permissions.py` asserts both the fix and that the
  underlying failure mode still exists.
- **A mode that cannot be set is reported, not ignored and not fatal.** See
  [docs/adr/0001](docs/adr/0001-warn-rather-than-refuse-on-unenforceable-permissions.md).
- **Absence is only inferred from a directory that was successfully read.**
  Pruning is scoped to the directories recorded in `scan_dirs`, and an
  unreadable directory is never checkpointed. The unscoped version deleted every
  row under a temporarily unmounted root and called them "no longer on disk";
  `tests/test_unreadable.py` guards it.
- **stdout is data, stderr is everything else**, so pipelines stay clean. For
  `cdm mcp` this is stricter: stdout is the protocol channel, and one stray
  `print()` corrupts it. `tests/test_mcp_server.py` checks that every stdout
  line is JSON-RPC with a raw pipe, because the SDK client tolerates garbage
  lines and would not catch it.
- **MCP tools that return names are registered only with `--expose-names`.**
  Not filtered — absent. Shape tools take no path arguments, and a test asserts
  that. See [docs/adr/0002](docs/adr/0002-mcp-exposes-shape-not-names-by-default.md).
- **The index's own files are never opened by a scan.** Closing any descriptor
  to a file drops every POSIX lock the process holds on it, SQLite's included;
  hashing `index.db-shm` mid-scan let another process delete the WAL under the
  writer (a SIGBUS). `tests/test_scan.py` reproduces it across two processes.
- **A vanished directory takes its whole subtree with it**, swept as orphans so
  indexes left inconsistent by older builds are repaired too, and never below a
  directory that could not be read.
- **Suggestions and the guide are advisory, and come from one engine each**
  (`suggest.py`, `guide.py`) that every front-end renders. Nothing runs a
  command for the user. A guide step is `done` only because the index shows it.
  See [docs/adr/0003](docs/adr/0003-suggestions-come-from-one-advisory-engine.md)
  and [docs/adr/0004](docs/adr/0004-guidance-is-a-checklist-whose-status-comes-from-the-index.md).
- **An access time is recorded only where it means "last read", and cdm's own
  reads never count.** Unknown is NULL, never a guessed date. The tests simulate
  a filesystem where reads move atime; without that they pass trivially on one
  where they do not (macOS, or Linux with `O_NOATIME`). See
  [docs/adr/0005](docs/adr/0005-access-time-is-recorded-only-where-it-means-last-read.md).
- **"Never opened" always travels with its date.** On macOS cdm's own first read
  spends the evidence, so it means "not opened between its last change and this
  date", never "unused"; groups use their earliest date. See
  [docs/adr/0006](docs/adr/0006-never-opened-is-recorded-with-the-date-it-was-last-true.md).
- **All MCP behaviour lives in `cdm/tools.py`, which is stdlib-only.**
  `cdm/mcp_server.py` only registers it with the SDK. That split is what lets
  the Python 3.9 CI job test the tools without the SDK, which needs 3.10+.
- **Anticipated MCP failures become `ToolError`.** The SDK deliberately hides
  the message of any other exception, so a helpful `ValueError` reaches the
  model as a bare "Error executing tool".

## Documentation

`man/cdm.1` is the reference; the README is the introduction. A new flag or
verb is not finished until it is in both. Lint the man page with:

```bash
mandoc -Tlint man/cdm.1
```

## Commits

Explain why, not what — the diff already says what. If a choice has a
non-obvious reason (a measurement, a filesystem behaviour, a locking
constraint), that reason belongs in the commit message or a comment, because it
is the thing nobody can reconstruct later.

## Releasing

See [docs/releasing.md](docs/releasing.md). The short version: bump the version
in `pyproject.toml`, tag it `v<version>`, push the tag. The workflow refuses a
tag that disagrees with the packaged version, publishes to TestPyPI first, then
installs that published wheel on a clean machine and runs it before the real
upload is even offered.

Do a `workflow_dispatch` dry run before any first-of-its-kind release. PyPI
never lets a filename be reused, so a mistake is permanent.
