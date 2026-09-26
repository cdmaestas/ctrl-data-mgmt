# 0005 — Access time is recorded only where it means "last read", and never counts cdm's own reads

**Context.** Suggestions (ADR 0003) judged staleness by mtime alone, so a model
loaded every day but never rewritten looked as cold as one abandoned a year
ago. Last access time is the obvious fix and is easy to get wrong in three
ways, each of which was checked on real systems before deciding:

1. Some filesystems never update it (`noatime`, read-only mounts).
2. Some update it only sometimes. Measured on macOS APFS: a read moves an atime
   that is older than the mtime and leaves one newer than the mtime alone, even
   when it is 50 days old. So on APFS the atime is "first read after the last
   change", and a model loaded daily keeps the date of its first load. Linux
   `relatime`, the default, also moves an atime more than a day old, so there
   it is "last read, to within a day".
3. cdm itself reads files — every newly hashed file, and every file
   `dupes --verify` confirms — and on most systems that moves the atime. The
   first `--checksum` scan would make everything look read today.

**Decision.** Record `atime` for files only (listing a directory updates its
atime, and cdm lists every directory it scans), and only where it means "last
read". Anything else is stored as NULL, meaning *unknown*, never as a date.

- **Trust is per device.** On the device holding cdm's data directory — the one
  place cdm may write — it is *measured*: a scratch file with a day-old atime
  newer than its mtime is read, and trust means the read moved it. On other
  devices it comes from the mount options: `noatime`, `ro` or `read-only` mean
  no; on macOS an unmeasured device is not trusted, because options cannot show
  APFS's rule; elsewhere a writable mount is trusted. A measurement beats the
  options in both directions.
- **cdm's reads never count.** Hashing opens files with `O_NOATIME` where the OS
  allows it (Linux, files you own). Otherwise, after reading a file cdm stores
  the atime its read left behind as `self_atime`; while the file's atime still
  equals it, nobody else has read the file, and the earlier `atime` stands.
  `dupes --verify` records its reads the same way.
- **Upgrading does not invent reads.** An index hashed before this change has no
  `self_atime`, so on the first scan after upgrading, an atime that falls
  inside an earlier hashing scan's time window (from the `scans` table; an
  unfinished scan counts for an hour) is recorded as unknown, and the next real
  read replaces it.

Where it is recorded, `atime` is used: `stat` shows it, `find` filters and
sorts on it (an unknown never matches), `age_histogram` takes `by='atime'` and
reports files without one separately, and `suggest` judges age by the later of
mtime and atime and shows each item's last read.

**Why.** A wrong "last read" is worse than none: it would make suggestions
confidently wrong in exactly the case the feature exists for. Measuring where
cdm can, and saying "unknown" where it cannot, keeps every date the index shows
true. Even a trusted atime is "read by something" — Spotlight, backups, virus
scanners all read files — so an old atime is good evidence of disuse, a recent
one is not proof of use, and nothing becomes `safe` on atime alone.

**Consequences.** On macOS the common case is "unknown", and `cdm doctor` says
how many files have a trusted access time so this is visible, not silent.
Filesystems that set atime policy outside the mount options — IBM Storage
Scale's `mmchfs -S`, for one — can be trusted wrongly from options alone; the
man page says so. Measuring more devices would mean writing into them, which
cdm does not do. The schema gains `atime` and `self_atime` (schema 4); ADR
0003's note that rules needing last access "wait until the index records it"
is resolved by this record.
