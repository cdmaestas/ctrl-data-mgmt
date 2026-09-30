# Test fixtures

Only **sanitizer output** belongs here. A policy listing from a real cluster
names every file on it — people's names, projects, customers — and an export
dump names every file on a host. None of that may enter a public repository.

The rules, and what enforces each:

| Rule | Enforced by |
|---|---|
| Raw inputs use a `.raw` suffix and live in `tests/fixtures/raw/` | `.gitignore` |
| Export dumps (`*.cdm-export*`) are never committed | `.gitignore` |
| Every file here except this README starts with `# sanitized by cdm-sanitize vN` | `scripts/check_raw_data.py` |
| No raw `.raw` file, `raw/` file or export dump is staged, even with `git add -f` | the pre-commit hook runs `scripts/check_raw_data.py --staged` |
| Nothing above is tracked, even after `git commit --no-verify` | CI runs `scripts/check_raw_data.py --all` |
| No file anywhere looks like raw `mmapplypolicy` LIST output without the header | both of the above |

Making a fixture from a real cluster (the sanitizer and its leak test arrive
with the policy listing format; until then no fixture is committed):

1. Run the sanitizer **on the cluster**. The raw listing never leaves it.
2. Copy only the sanitized file here, and delete the raw one on the cluster.
3. Read the whole fixture before committing it: paths must be placeholders,
   and no cluster, filesystem, fileset, pool or node name may appear.
4. Commit. The leak test checks that none of the sanitizer's input tokens
   survived.

If the hook stops a commit, unstage the file with `git reset HEAD <file>`;
don't bypass the hook, because CI will reject the same file.
