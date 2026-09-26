# 0004 — Guidance is a fixed checklist whose status comes from the index

**Context.** Having `cdm` installed did not tell anyone what to do with it. The
README went from install to a table of verbs, and the first real use hit the
gaps: whether to hash, what to run after deleting something, how often to
rescan, when an AI client is worth connecting. Suggestions (ADR 0003) answer
"what is worth doing" once there is a hashed index, and nothing before that.
A UI is planned and should offer wizard-style guidance, and the CLI and the MCP
server should give the same guidance rather than three different ones.

**Decision.** `cdm/guide.py` defines one fixed, ordered checklist, and every
front-end renders it: `cdm guide` prints it with the next step expanded, the MCP
`guide` tool returns it and a `getting-started` prompt walks a client through
it, and a UI shows it as a wizard. The steps are: index a folder; hash it; see
what's worth doing; clean up, then rescan; keep it current; optionally connect
an AI client. Each step carries its title, why it matters, the command, what
the user will see after running it, a status, and a one-line fact from the
index ("100% of files hashed", "2 roots not scanned in 7+ days").

**Status is observed, never asserted.** A step is `done` only because the index
shows it: a root exists, at least 90% of its files are hashed, every root was
scanned within 7 days. Nothing is marked done because someone pressed Next or
said so. A step the index cannot observe — whether a suggestion was acted on,
whether an AI client is connected — is `advice` or `optional` and never shows
as done; a front-end that wants to know asks the user. `next` is always set: the
first step still to do, otherwise the first piece of advice, so a wizard never
reaches a blank screen. Progress counts only the always-checkable steps (index,
hash, keep current), so "0 of 3" becomes "3 of 3" without the total moving.

**Advisory, like suggestions.** Nothing in the guide runs a command. Anything
that installs something lasting is printed for a person to install: `cdm guide
--schedule` writes a launchd agent or cron line to stdout (so it can be
redirected into place) and the install and removal instructions to stderr.
The guide never edits a crontab or loads an agent.

**Names.** Root paths appear, since the user chose them; nothing below a root
does, and suggestion counts are taken with names off. The guide is therefore a
shape tool, available without `--expose-names`.

**Why.** Deriving status from the index means the guide cannot drift from
reality: delete the index and it starts over at step one; let a root go stale
and "keep it current" reopens by itself; a UI wizard, the CLI and a chat client
all agree because none of them keeps its own notion of progress. Keeping the
checklist fixed and short keeps it a map rather than a manual — each step links
to the command that does the work, and depth stays in the man page.

**Consequences.** A new step needs an observable "done" condition, or it is
advice. Front-ends may style the steps however they like but must not add
local state that marks a step done. Installing a schedule automatically, or
any other guide step that would act rather than advise, needs its own decision
record.
