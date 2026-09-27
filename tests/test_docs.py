"""Documentation cannot drift from the command line.

The man page is hand-written, so every option of every verb must appear in it.
The pre-commit hook already checks the verbs; this checks their options, which
is where drift actually happened (new flags documented in the README but not
in cdm(1), or the reverse).
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from cdm.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
MAN = (ROOT / "man" / "cdm.1").read_text()
# roff escapes hyphens as \-; compare in plain text.
MAN_TEXT = MAN.replace("\\-", "-")


def _options():
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for verb, p in sub.choices.items():
        for action in p._actions:
            for opt in action.option_strings:
                if opt.startswith("--") and opt != "--help":
                    yield verb, opt


def test_every_option_of_every_verb_is_in_the_man_page():
    missing = sorted({f"{verb} {opt}" for verb, opt in _options()
                      if not re.search(rf"(?<![\w-]){re.escape(opt)}(?![\w-])", MAN_TEXT)})
    assert not missing, f"options missing from man/cdm.1: {missing}"
