"""What the scan refuses to record.

An index that quietly omits things is worse than one that refuses loudly, so
every skip is counted and reported back to the caller for printing.

Credential paths are excluded by default. The point of this tool is to tell you
what you own; it is not to build a convenient list of where your keys live.

`.gitignore` is deliberately NOT honoured. It describes what git should ignore,
not what exists on your disk -- a catalog that hides your build outputs is
lying about your disk usage, which is one of the questions the catalog is for.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

# Matched against a single path component (directory or file name).
CREDENTIAL_NAMES = frozenset({
    ".ssh",
    ".gnupg",
    ".pki",
    ".aws",
    ".kube",
    ".docker",
    ".password-store",
    ".gcloud",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "Keychains",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "service-account.json",
})

# Matched against the whole path with fnmatch.
CREDENTIAL_GLOBS = (
    "*/Library/Keychains/*",
    "*/Library/Application Support/Google/Chrome/*",
    "*/Library/Application Support/Firefox/*",
    "*/.mozilla/firefox/*",
    "*/.config/google-chrome/*",
    "*/.config/gcloud/*",
    "*/.local/share/keyrings/*",
    "*.pem",
    "*.key",
    "*_rsa",
    "*_ed25519",
)


class Excluder:
    """Decides whether a path is recorded, and remembers why not."""

    def __init__(self, extra: tuple[str, ...] = (), skip_credentials: bool = True):
        self.extra = tuple(extra)
        self.skip_credentials = skip_credentials
        self.skipped_credentials = 0
        self.skipped_user = 0

    def excludes(self, path: Path) -> bool:
        name = path.name
        text = str(path)

        if self.skip_credentials:
            if name in CREDENTIAL_NAMES or any(
                fnmatch.fnmatch(text, g) for g in CREDENTIAL_GLOBS
            ):
                self.skipped_credentials += 1
                return True

        for pattern in self.extra:
            if fnmatch.fnmatch(text, pattern) or fnmatch.fnmatch(name, pattern):
                self.skipped_user += 1
                return True

        return False

    def report(self) -> list[str]:
        """Human-readable lines describing what was left out. Never silent."""
        lines = []
        if self.skipped_credentials:
            lines.append(
                f"skipped {self.skipped_credentials} credential path(s) "
                f"(--no-skip-credentials to include)"
            )
        if self.skipped_user:
            lines.append(f"skipped {self.skipped_user} path(s) matching --exclude")
        return lines
