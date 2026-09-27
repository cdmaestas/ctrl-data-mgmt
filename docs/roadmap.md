# Roadmap

What each phase is for, what is in it, and what is deliberately left out. A
phase is written here once it has been worked through in detail; its individual
tasks then become GitHub issues under a milestone of the same name. Phases not
yet worked through stay here as intent, not commitments.

Decisions made while building a phase go in `docs/adr/`, as before. This file
says *what* and *why this order*; the ADRs say *how* and *why this way*.

**Version numbers are milestone names, not tags.** The release workflow
publishes to PyPI on any pushed `v*` tag, so nothing is tagged until the
packaging phase sets publishing up (see Phase 4 and the backlog).

| phase | version | theme | status |
|---|---|---|---|
| 1 | 0.1.0 | Local catalog, suggestions, guide, access times | done, not published |
| 2 | 0.2.0 | IBM Storage Scale at scale | planned in detail, issues open |
| 3 | 0.3.0 | A UI over the guide and suggestions | intent |
| 4 | 0.4.0 | Packaging for Linux, macOS and Windows | intent |
| — | 1.0 | Index schema and command line promised stable | after phase 4 |

## Phase 1 — 0.1.0: local catalog (done)

Everything in the [CHANGELOG](../CHANGELOG.md): scans with partial and full
hashes, `find`/`du`/`dupes`/`stat`, nested roots, the read-only MCP server with
names opt-in, `cdm suggest`, `cdm guide`, a nightly rescan to install yourself,
last-access time where it is trustworthy, and "never opened since it changed".
Built and verified on macOS and on RHEL 9 with XFS and Storage Scale. Not
published: publishing belongs to Phase 4.

## Phase 2 — 0.2.0: IBM Storage Scale at scale

**Who it is for.** Proven on our own clusters first, written for Storage Scale
administrators generally: documentation, safety around privilege and other
people's file names, and a runbook, not just working code.

**Why this, and why now.** A POSIX walk is the wrong tool on GPFS however many
nodes run it (see [multi-host.md](multi-host.md)). The policy engine reads
inode metadata directly, orders of magnitude faster. Feeding its output into the
same index gives every Phase 1 feature — suggestions, the guide, access times —
at cluster scale.

**In scope**

- **Policy generation, not execution.** cdm writes the `mmapplypolicy` rules and
  prints the exact command; an administrator runs it. cdm never needs root and
  never runs anything on the cluster, the same advisory pattern as
  `cdm guide --schedule`. The generated policy covers **one fileset by
  default**; a whole filesystem needs `--whole-filesystem`, which warns that the
  index will then hold every user's file names. The wrapper records the
  filesystem's `mmlsfs -S` setting and ends the listing with a row count.
- **`cdm import --policy FILE`.** Reads the LIST output into the same index,
  under a logical host name `<filesystem>@<cluster>` so listings taken from any
  node line up.
  - **A complete snapshot.** Within the imported scope, rows missing from the
    listing are removed — only if the listing is verifiably complete (every line
    read, row count matches). A truncated or failed import changes nothing and
    says so. This is Phase 1's rule that absence is only inferred from a
    complete listing.
  - **Access times by Phase 1's rules.** `-S relatime` or `no` means a last
    read; `yes` means unknown. "Never opened since it changed" from access time
    older than modification time. The policy engine reads no file contents, so
    cdm's own reads are not an issue here.
  - **Storage Scale fields**: fileset and storage pool, usable in `find` and
    `suggest`.
- **`cdm hash`.** A pass that hashes only files sharing a size with another, so
  `dupes` works on imported trees without reading everything. Reuses Phase 1's
  hash rules.
- **`cdm export` / `cdm merge`**, for what one policy scan cannot see: each
  node's local storage, workstations, and network filesystems without a policy
  engine (NFS, SMB — each scanned by one host under a logical name, never by
  every node that mounts it). Dumps are newline-delimited JSON, one complete
  snapshot per (host, root) with an end marker; a merge replaces only that
  host and root, never anyone else's rows. `merge` refuses GPFS-imported rows:
  GPFS goes through import. The central index lives on local disk, never on a
  shared filesystem.
- **One pool-aware suggestion.** Data neither modified nor read in a long time,
  sitting on the fastest pool, with a draft `MIGRATE` rule for an administrator
  to review and run. Advisory, like every suggestion.
- **Testing without a cluster in CI**
  - A fixture recorded from a real cluster and **sanitized on the cluster**:
    paths become placeholders (shape and common extensions kept), UID/GID
    renumbered, cluster, filesystem, fileset and pool names replaced, node
    names removed. The raw file never leaves the cluster. A person reviews the
    fixture before it is committed, and a test proves none of the sanitizer's
    input tokens survive.
  - **Guards against committing raw data.** Raw inputs use a `.raw` suffix
    under `tests/fixtures/raw/`, both ignored by git, as are export dumps.
    Sanitized fixtures carry a `sanitized by` header. The pre-commit hook and CI
    reject staged raw files, export dumps and any fixture without the header, so
    `git add -f` and `--no-verify` do not get one through.
  - A synthetic generator for large listings, for the speed target.
  - A repeatable check on a real cluster over SSH, with its results in each PR.

**Done when**

1. A whole-filesystem import of a real cluster (zimafs1) works end to end, and
   `find`, `du`, `suggest` and `guide` all work on the imported rows.
2. `cdm hash` makes `dupes` work on them.
3. Two hosts' indexes merge into one.
4. Import sustains at least 1 million rows per minute on a synthetic 10-million
   line listing.
5. An administrator's runbook is in the docs.
6. Each piece is proven on a real cluster, not only in tests.

**Out of scope for Phase 2** (see the backlog): running `mmapplypolicy` through
RBAC or the REST API, sharded POSIX walks, and anything that acts rather than
advises.

## Phase 3 — 0.3.0: a UI

A UI over the engines that already exist: the guide as a wizard, suggestions as
cards. The rules are in ADRs 0003 and 0004 — it renders their output, keeps no
state of its own that marks a step done, and never runs a suggested command
without a decision record that allows it.

**First decision when this phase is worked through:** a native desktop app, or a
local web page served by `cdm ui`. It decides how much packaging a UI rewrite
would cost, which is why the UI comes before packaging.

## Phase 4 — 0.4.0: packaging for Linux, macOS and Windows

- **Publishing.** PyPI via the existing release workflow, once Trusted
  Publishing is set up (see the backlog), and the first `v*` tag.
- **Native packages**: Homebrew, rpm/deb, Windows installers, possibly
  standalone binaries.
- **Windows port.** Not only packaging: path handling (cdm's path ranges assume
  `/`), file permissions, scheduling (Task Scheduler rather than launchd or
  cron), and access-time behaviour (NTFS) all need Windows versions.

## Backlog

Not yet in a phase. Each needs working through before it is scheduled.

- **Run `mmapplypolicy` through Storage Scale RBAC and the native REST API**, as
  an alternative to an administrator running the generated command.
- **PyPI Trusted Publishing setup.** A one-time configuration on pypi.org that
  the release workflow depends on; expected to take time, and a prerequisite
  for Phase 4 publishing.
- **Branch protection on `main` requiring the CI checks**, so pull requests can
  use GitHub auto-merge.
- **Sharded POSIX walks** (`cdm scan --shard N/TOTAL`) for large non-GPFS trees.
- **Restoring access times after hashing** on macOS, so "never opened" stays
  current. Rejected for now because cdm would write to users' files; needs its
  own ADR (see ADR 0006).
