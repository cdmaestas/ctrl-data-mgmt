# Multi-host scanning with pdsh

**Status: design note. Nothing here is implemented.** The `host` column exists
in the schema; everything else below is a plan, recorded so the v1 decisions
that make it cheap don't get undone by accident.

Reference: [chaos/pdsh](https://github.com/chaos/pdsh), GPL-2.0.

## What pdsh gives us

`pdsh` runs a command on many hosts in parallel. The `exec` rcmd module is the
relevant one — it "executes an arbitrary command for each target host", with
the first remote argument being the local command to run:

```bash
pdsh -R exec -w node[1-64] ssh -x -l %u %h cdm scan /local/scratch
```

Substitutions available in the exec module:

| token | expands to |
|---|---|
| `%h` | target hostname |
| `%u` | remote username |
| `%n` | target rank, `0..n-1` |
| `%%` | a literal `%` |

`-w` takes a hostlist (`node[1-64]`, files, filters), and `-f` sets the fanout
— the number of commands running at once, default 32.

## Two different uses, often confused

**Fan-out across machines.** Each node scans its own local storage and returns
results. This is the obvious one, and it is what `host` is for.

**Rank-sharding one tree.** `%n` gives each invocation an index, which means
`pdsh -R exec` can drive N parallel workers over a *single* filesystem with no
remote hosts involved at all. Useful on a shared mount that one thread cannot
walk fast enough.

These need different things from `cdm`, and only the first needs a `host`
column. Don't build one and assume it covers the other.

## The constraint that shapes the design

**The index must never live on the shared filesystem.** SQLite's locking relies
on POSIX advisory locks behaving correctly, which is not dependable over NFS and
is a bad bet on GPFS or Lustre. N nodes writing one index file over a parallel
filesystem is how you get a corrupted database, and it will corrupt
intermittently under load rather than fail cleanly in testing.

So the model is **scan local, merge central**, not **write shared**:

1. Each host scans to its own local index, on local disk.
2. Each host emits a portable dump of its rows.
3. A collector merges the dumps into one index, keyed by `(host, path)`.

That is why `(host, path)` is the primary key rather than `path` alone, and why
`host` is populated from day one even though nothing reads it yet.

## What would need building

- `cdm export [--root PATH]` — write rows to stdout in a stable format that
  survives being piped through `pdsh`. Newline-delimited JSON is the obvious
  choice; pdsh prefixes each output line with `hostname: `, so either strip that
  on the collector side or have pdsh write per-host files with `-P`.
- `cdm merge FILE...` — insert dumps into the local index, replacing any rows
  for the same `(host, path)`.
- A rank-shard flag for the second use case — something like
  `cdm scan PATH --shard N/TOTAL`, splitting on top-level subdirectories rather
  than a hash of the path, because the file list isn't known until the walk has
  already happened.
- `CDM_HOST` already exists to override the recorded hostname. On a shared
  filesystem that matters: forty nodes scanning the same mount should record one
  logical name, not forty, or dedupe will report every file forty times over.

## The thing that probably makes this moot on GPFS

On Spectrum Scale, a POSIX walk is the wrong tool regardless of how many nodes
run it in parallel. `mmapplypolicy` reads inode metadata directly and produces
a listing orders of magnitude faster than `scandir` across any number of
workers.

If the target is GPFS, the higher-value path is not pdsh fan-out at all — it is
**treating policy-engine output as an input format**: `cdm import --policy FILE`
parsing an `mmapplypolicy` LIST output into the same schema. Same index, same
queries, none of the crawling. Worth settling which of these two matters before
either is built.

## Licensing

pdsh is GPL-2.0; this project is Apache-2.0. Invoking `pdsh` as a subprocess is
not linking and creates no derivative work, so there is no conflict in
*calling* it. Do not vendor pdsh source into this tree.
