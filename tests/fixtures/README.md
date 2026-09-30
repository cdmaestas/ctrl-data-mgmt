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

Making a fixture from a real cluster:

1. On the cluster, list a test fileset with `cdm policy` (see `cdm(1)`,
   STORAGE SCALE) and run the script as root. Use a fileset holding only test
   data where you can.
2. Still on the cluster, sanitize it. Copy `scripts/` and `cdm/` over; nothing
   needs installing:

   ```bash
   sudo python3 scripts/sanitize_listing.py fs1-proj.list.raw fs1-proj.list
   ```

   It replaces every path component with a placeholder (`d0007`, `f0412`,
   `l0003`), keeping the tree's shape, hidden-name dots and common extensions;
   renumbers UIDs and GIDs from 1000; and replaces the cluster, filesystem,
   fileset and pool names. Inodes, sizes, times and modes are kept.
3. The sanitizer then checks its own output for **every name in the input** —
   collected from the input itself, not from the code that replaced them — and
   writes nothing if one survived. This is the leak test that matters, because
   it runs where the raw listing is. It refuses a `.raw` output name and
   overwriting its input.
4. Copy only the sanitized file here, and delete the raw one on the cluster.
5. Read the whole fixture before committing it. `tests/test_sanitize.py` then
   checks its structure in CI: every name in it must be one the sanitizer
   writes, and every UID, pool and fileset must be renumbered.

If the hook stops a commit, unstage the file with `git reset HEAD <file>`;
don't bypass the hook, because CI will reject the same file.
