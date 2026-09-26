# 0003 — Suggestions come from one advisory engine that every front-end renders

**Context.** Answering "what is eating my disk" only works if you already know
which question to ask. Through MCP that meant knowing the tool names; at the
command line, knowing that `du`, `dupes` and `find` had to be combined. Users
asked for the index to say what is worth doing. There will be more than one
front-end — `cdm suggest`, the MCP server, and possibly a UI later — and if
each one grew its own idea of "worth doing" they would disagree about the same
index, and each would need its own privacy review.

**Decision.** `cdm/suggest.py` is the only place that decides what to suggest.
It is stdlib-only core code, like `shape.py`, and returns plain data: a ranked
list of suggestions, each with an id, a title, the reason, the bytes involved,
a **risk** (`none` for index housekeeping, `safe` for data that regenerates on
its own such as a package cache, `review` for everything else) and the
**command** a person would run. Front-ends only render that list. `cdm suggest`
prints it; the MCP `suggest` tool returns it; a future UI shows it.

The engine and every front-end are **advisory**. Nothing deletes, and no
front-end may run a suggested command on its own: the MCP tool description and
server instructions both tell the model to show commands, not execute them. The
rules are conservative by design:

- Anything judged by modification time is at most `review`. The index records
  mtime, not last access, so "not modified in 90 days" is not "not used in 90
  days" — a model loaded daily but never rewritten looks cold. The output says
  so every time.
- Dependencies and build output count only inside a git working tree, where
  "rebuild it" is actually true. An installed editor extension also has a
  `package.json` beside `node_modules` and `dist`; nothing will rebuild it.
- A cache that contains a more specific, separately listed cache is credited
  only with the remainder, so one cache is never counted twice. Savings can
  still overlap *between* rules (a duplicate inside a cache), and the output
  says that rather than printing a total.
- Index housekeeping (a stale scan, an unhashed root, stale hashes) ranks
  first, because it decides whether every other answer is right.

Names follow the same switch as the MCP name tools (ADR 0002). With names off,
a suggestion carries its title, reason, size, risk and — where the command
needs no path — the command, but no path below a root; the fixed rule
vocabulary ("npm cache", "Large git histories") comes from `suggest.py`, never
from the index. `build_server` sets the catalog's names switch itself, so which
tools exist and what `suggest` may say can never disagree.

The same change makes the MCP server easier to start with. It offers
**prompts** — ready-made questions such as "What can I clean up?" that clients
list as a menu (Claude Code shows them as slash commands) — and every tool
result carries **`next_steps`**, one or two pointers to the next useful tool.
Both are fixed text in `tools.py`, tested SDK-free, and never mention a name
tool unless names are exposed.

**Why.** One engine means one set of rules to test and one privacy boundary to
review, and a new front-end gets correct behaviour by rendering a list instead
of reimplementing judgement. Advisory-only keeps `cdm` a read-only catalog: the
worst a wrong rule can do is suggest something a person then declines. Being
conservative costs some missed savings (a `node_modules` outside a checkout is
never suggested); being wrong costs someone's data, so that trade is deliberate.

**Consequences.** New rules go in `suggest.py` with tests. A front-end that
wants to *act* on a suggestion — a UI "clean up" button — needs its own
decision record first: this one does not grant it. Rules that would need data
the index does not have (last access time, which process owns a cache) wait
until the index records it.
