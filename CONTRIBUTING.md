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
- **stdout is data, stderr is everything else**, so pipelines stay clean.

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
