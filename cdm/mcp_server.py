"""`cdm mcp`: the tool catalogue, registered with the MCP SDK over stdio.

Deliberately thin. Every behaviour lives in tools.py, which is stdlib-only and
tested on every supported Python; this module only needs the SDK (Python 3.10+,
installed by the `[mcp]` extra) and does nothing but registration.

Name tools are registered or not at all. There is no filtering step to get
wrong: without --expose-names, `find`, `du`, `dupes` and `stat` do not exist as
far as the client can tell.
"""
from __future__ import annotations

import functools
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.prompts.base import Prompt
from mcp.types import ToolAnnotations

from . import __version__
from .db import IndexUnavailable
from .tools import NAME_TOOLS, PROMPTS, SHAPE_TOOLS, Catalog, prompt_text

# Failures the model caused or can act on. The SDK passes a ToolError's message
# through to the client and deliberately HIDES the message of anything else, so
# without this translation "not an indexed root; known roots are ..." reaches the
# model as a bare "Error executing tool summary" -- a failure it cannot correct.
_ANTICIPATED = (ValueError, IndexUnavailable)


def _surface_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _ANTICIPATED as exc:
            raise ToolError(str(exc)) from exc
    return wrapper

# Advertised to clients so they can treat every call as safe to make freely.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)

INSTRUCTIONS = (
    "Answers questions about a local file metadata catalog built by `cdm scan`. "
    "The index is a snapshot: check `summary` for how long ago each root was "
    "scanned before treating an answer as current. Sizes are binary (1K = 1024). "
    "For 'what should I do' or 'what can I clean up', call `suggest`; its "
    "commands are for the user to run, never for you to run. Each tool result "
    "has `next_steps` suggesting where to go next. "
    "If the only tools available are summary, histograms, extensions and "
    "duplicates_summary, the user has chosen not to share file or directory "
    "names; do not ask for them."
)


def build_server(catalog: Catalog, *, expose_names: bool) -> MCPServer:
    server = MCPServer(name="ctrl-data-mgmt", version=__version__,
                       instructions=INSTRUCTIONS)
    # One switch for both halves of the names contract: which tools exist, and
    # whether `suggest` may list paths. Set here so they can never disagree.
    catalog.expose_names = expose_names
    tools = dict(SHAPE_TOOLS)
    if expose_names:
        tools.update(NAME_TOOLS)
    for name, description in tools.items():
        server.add_tool(_surface_errors(getattr(catalog, name)), name=name,
                        description=description, annotations=READ_ONLY,
                        structured_output=True)
    for name, (title, description, _, _) in PROMPTS.items():
        server.add_prompt(Prompt.from_function(
            _prompt(name, expose_names), name=name, title=title,
            description=description))
    return server


def _prompt(name: str, expose_names: bool):
    def render(root: str = "") -> str:
        """`root` optionally narrows the question to one indexed root."""
        return prompt_text(name, expose_names, root or None)
    return render


def serve(*, expose_names: bool, catalog: Catalog | None = None) -> int:
    catalog = catalog or Catalog(expose_names=expose_names)
    for line in catalog.banner(expose_names):
        print(line, file=sys.stderr)
    build_server(catalog, expose_names=expose_names).run(transport="stdio")
    return 0
