# 0001 — Warn, rather than refuse, when the index mode cannot be enforced

**Context.** The index must be `0600` in a `0700` directory, because it lists
every filename you own. `chmod` can fail on filesystems that do not support it,
and this tool is aimed at people who point it at exactly those — network mounts,
container overlays, shared cluster storage.

**Decision.** `cdm` verifies the mode after setting it, and on failure prints a
warning naming the file and continues. It does not refuse to run. `cdm doctor`
exits non-zero in the same situation so a script can gate on it.

**Why.** Refusing would make the tool unusable on filesystems where the user may
not care (a single-user machine, a scratch mount nobody else can reach), and
silence would break the privacy claim the README, man page and CI all assert.
Reporting loudly and letting the operator decide is the only option that neither
lies nor blocks. This is hard to reverse: tightening it to a hard failure later
breaks anyone who came to rely on the warning.
