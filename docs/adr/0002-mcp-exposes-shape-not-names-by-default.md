# 0002 — The MCP server exposes the shape of the data, not its names, by default

**Context.** `cdm mcp` lets an AI client answer questions about the index. With
a cloud-hosted model, everything a tool returns goes to the model's provider,
and this is a public tool whose whole premise is that nothing leaves the
machine. Filenames are often sensitive on their own: project codenames, people's
names, case or patient numbers baked into paths.

**Decision.** By default the server registers only tools that return shape:
per-root totals, size and age histograms, file extensions, and duplicate
totals. Root paths appear (the user typed them); nothing below a root does.
Tools that return paths — `find`, `du`, `dupes`, `stat` — exist only when the
server is started with `--expose-names`, and are then not filtered but simply
present. Without the flag they are never registered, so a client cannot call
them or learn that they exist. The server opens the index read-only and never
walks directories or reads file contents.

**Why.** Shape answers most real questions — what is eating the disk, how much
is cold, how much is duplicated — without sending a single name. Gating at
registration rather than filtering output means there is no code path, error
message included, that must remember to strip a path. The residual limit is
extensions: a short alphanumeric suffix shared by two or more files is shown as
a file type even if it is really a word from their names. That limit is
accepted, and tested, rather than hidden. This is hard to reverse, because
loosening the default later would silently start sending names for every user
who never passed the flag.
