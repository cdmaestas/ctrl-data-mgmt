# 0006 — "Never opened since it changed" is recorded with the date it was last known true

**Context.** ADR 0005 records a last-read time only where every read moves the
atime. On macOS APFS that is not so: measured, only the first read after a
change moves it. That still answers a narrower, useful question — has this file
been opened at all since it was downloaded or last written? — which is what
tells an installer never run, or a model downloaded and never loaded, from one
in use. But the answer is one-shot: the first read of any kind spends it, and
cdm's own `--checksum` read is a read. macOS has no `O_NOATIME`. So once cdm
hashes a never-opened file, a later first open by the user is invisible.

The options were: record it with the date it was last known true; restore the
atime after hashing, keeping the signal live but writing metadata to user files
(ctime changes, ownership needed) and breaking "cdm never touches data on
disk"; skip hashing unopened files, keeping the signal but losing them from
duplicate detection; or not build it.

**Decision.** Record it, dated. Each file gets `unopened_until`: the file was
not opened between its last change and that time. NULL means opened since, or
unknown. It is kept wherever the first read after a change moves the atime —
filesystems of mode LAST (every read counts) or FIRST (only the first does) in
`atime.py`'s terms — and the probe now tells those two, and NONE, apart.

- **Observed directly** when the atime is older than the mtime, judged from the
  stat taken before cdm's own read. The date is the scan's start.
- **Carried forward** while only cdm has read the file since (its atime still
  equals `self_atime`). On LAST the date advances to each new scan, since a
  later read would have shown; on FIRST it stays where it was, because cdm's
  read spent the evidence. A change to the file starts the question over.
- **Recovered on upgrade** for indexes hashed before this existed. On a FIRST
  filesystem, an earlier cdm read could only have moved the atime of a file
  that was unopened at that moment, so an atime inside an earlier hashing
  scan's window means "unopened as of that scan's start". On LAST no such
  inference holds, and the value stays unknown.

Front-ends always show the date: `stat` ("not since it last changed, as of
…"), `find --unopened` and MCP `find(unopened=true)` with `unopened_as_of` on
each result, and `suggest`, which marks installers and model stores "never
opened (as of …)" using the earliest date in a group so a group never claims
more than its weakest evidence. `doctor` says what each root's filesystem
records.

**Why.** It is the only option that keeps cdm read-only and keeps every file in
duplicate detection, and it states exactly what is known: never "unused", only
"not opened between these two dates". On Linux, where cdm reads with
`O_NOATIME` and relatime keeps working, the date stays current. On macOS it is
most valuable for files cdm sees before anyone opens them — new downloads — and
it goes stale for the rest, visibly, because the date is shown.

**Consequences.** Schema 5 adds `unopened_until`. A front-end must show the
date with the flag. Restoring atimes after hashing remains an option only with
its own decision record, since it would make cdm write to user files.
