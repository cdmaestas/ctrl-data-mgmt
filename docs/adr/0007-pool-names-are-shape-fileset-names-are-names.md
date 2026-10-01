# 0007 — Storage pool names are shape; fileset names are names

**Context.** Imported Storage Scale listings carry each file's storage pool and
fileset, and the MCP server can now report totals by both. ADR 0002 decided
that by default the server returns the *shape* of the data — sizes, ages, file
types — and no name below a root, because with a cloud-hosted model everything
it returns goes to the provider. The Phase 2 plan assumed pool and fileset
names were both shape, being chosen by an administrator rather than by users.
On a real cluster that held for pools and not for filesets: the fileset in use
was named after its user. Fileset names are commonly a person's name, a
project's, or a customer's codename — exactly what ADR 0002 keeps back.

**Decision.** Storage pool names are shape and are returned by default: they
name storage tiers (`system`, `data1`, `ssd`) and say where data sits, not whose
it is. Fileset names are names: without `--expose-names` the MCP `storage` tool
reports filesets by rank of size (`fileset #1` is the largest), with their
file counts and bytes, and never their names; with `--expose-names` it names
them, and `find` (a name tool) can filter on them. The command line always
shows both, since its user owns the index.

**Why.** Ranking keeps the useful shape — how many filesets, how lopsided —
while dropping the part that identifies people. Pools carry no such risk and
are the point of tiering advice (the pool-aware suggestion in #14), so hiding
them would cost usefulness for no privacy gain. This follows ADR 0002's rule
that the default must never be loosened silently: a fileset name reaching the
model is something a user opts into, never something that starts happening.

**Consequences.** Anything new that reports filesets through a shape tool must
rank, not name, them unless names are exposed. If a site's pool names turn out
to be sensitive, that needs revisiting here, not a quiet exception in code.
