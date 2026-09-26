"""The MCP adapter, against the real SDK.

Skipped where the SDK cannot be installed (Python 3.9). Everything the adapter
does beyond registration is covered SDK-free in test_tools.py; these tests prove
the registration itself, and one test drives a real stdio round trip, because
the failure that matters most -- anything stray on stdout corrupting the
protocol -- only shows up with a real client on the other end of the pipe.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import pytest

pytest.importorskip("mcp")

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from cdm import cli, db, hashing, mcp_server, tools  # noqa: E402
from cdm.scan import scan_root  # noqa: E402

HOST = "testhost"


@pytest.fixture()
def catalog(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.csv").write_text("hello")
    (root / "b.csv").write_text("hello")
    index = tmp_path / "index.db"
    conn = db.connect(index)
    scan_root(conn, HOST, root, hash_kind=hashing.PARTIAL)
    conn.close()
    return tools.Catalog(index=index, host=HOST)


def listed(server):
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_name_tools_do_not_exist_without_the_flag(catalog):
    names = set(listed(mcp_server.build_server(catalog, expose_names=False)))
    assert names == set(tools.SHAPE_TOOLS)


def test_name_tools_exist_with_the_flag(catalog):
    names = set(listed(mcp_server.build_server(catalog, expose_names=True)))
    assert names == set(tools.SHAPE_TOOLS) | set(tools.NAME_TOOLS)


def test_every_tool_is_advertised_read_only(catalog):
    for tool in listed(mcp_server.build_server(catalog, expose_names=True)).values():
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name


def test_results_are_structured_with_a_published_schema(catalog):
    server = mcp_server.build_server(catalog, expose_names=False)
    assert all(t.output_schema for t in listed(server).values())
    result = asyncio.run(server.call_tool("summary", {}))
    assert result.structured_content["total_files"] == 2


def test_argument_schemas_survive_the_error_wrapper(catalog):
    """functools.wraps must keep the real signature visible to the SDK."""
    find = listed(mcp_server.build_server(catalog, expose_names=True))["find"]
    assert {"name", "iname", "larger_than", "limit"} <= set(find.input_schema["properties"])
    du = listed(mcp_server.build_server(catalog, expose_names=True))["du"]
    assert du.input_schema["required"] == ["path"]


def test_prompts_are_offered_and_match_the_names_setting(catalog):
    for expose in (False, True):
        server = mcp_server.build_server(catalog, expose_names=expose)
        offered = {p.name: p for p in asyncio.run(server.list_prompts())}
        assert set(offered) == set(tools.PROMPTS)
        assert all(p.title for p in offered.values())
        got = asyncio.run(server.get_prompt("cleanup", {"root": "/r"}))
        text = got.messages[0].content.text
        assert "`suggest` with root='/r'" in text
        assert ("`du`" in text) is expose


def test_build_server_decides_what_suggest_may_name(catalog):
    mcp_server.build_server(catalog, expose_names=True)
    assert catalog.expose_names is True
    mcp_server.build_server(catalog, expose_names=False)
    assert catalog.expose_names is False


def test_an_anticipated_error_reaches_the_model_with_its_message(catalog):
    """The SDK hides the text of unexpected exceptions; ours must not be hidden."""
    server = mcp_server.build_server(catalog, expose_names=False)
    with pytest.raises(ToolError, match="Known roots"):
        asyncio.run(server.call_tool("summary", {"root": "/not/a/root"}))


def test_cdm_mcp_refuses_to_start_without_an_index(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CDM_DATA_DIR", str(tmp_path / "empty"))
    assert cli.main(["mcp"]) == 1
    captured = capsys.readouterr()
    assert "cdm scan" in captured.err
    assert captured.out == ""


def test_stdio_round_trip_keeps_stdout_for_the_protocol(tmp_path):
    """A real client, a real subprocess, a real pipe."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    root = tmp_path / "root"
    root.mkdir()
    (root / "x.csv").write_text("1")
    env = dict(os.environ, CDM_DATA_DIR=str(tmp_path / "data"), CDM_HOST=HOST)
    conn = db.connect(tmp_path / "data" / "index.db")
    scan_root(conn, HOST, root)
    conn.close()

    async def session():
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "cdm", "mcp"], env=env)
        with tempfile.TemporaryFile("w+") as errlog:
            async with stdio_client(params, errlog=errlog) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    names = [t.name for t in (await s.list_tools()).tools]
                    ok = await s.call_tool("summary", {})
                    bad = await s.call_tool("summary", {"root": "/nope"})
            errlog.seek(0)
            return names, ok, bad, errlog.read()

    names, ok, bad, stderr = asyncio.run(asyncio.wait_for(session(), timeout=60))
    assert set(names) == set(tools.SHAPE_TOOLS)
    assert ok.is_error is False and ok.structured_content["total_files"] == 1
    assert bad.is_error is True and "Known roots" in bad.content[0].text
    assert "names are NOT exposed" in stderr


def test_every_stdout_line_is_json_rpc(tmp_path):
    """stdout is the protocol channel; nothing else may appear on it.

    Checked with a raw pipe rather than the SDK client, because the client is
    lenient: it logs a line it cannot parse and carries on, so a stray print()
    would not fail a session-level test for the right reason. (Verified: moving
    the banner to stdout left the SDK session working.)
    """
    import json
    import subprocess

    root = tmp_path / "root"
    root.mkdir()
    (root / "x.csv").write_text("1")
    conn = db.connect(tmp_path / "data" / "index.db")
    scan_root(conn, HOST, root)
    conn.close()

    initialize = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "cdm-test", "version": "0"}}})
    proc = subprocess.run(
        [sys.executable, "-m", "cdm", "mcp"], input=initialize + "\n",
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, CDM_DATA_DIR=str(tmp_path / "data"), CDM_HOST=HOST))

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, f"no response on stdout; stderr was:\n{proc.stderr}"
    for line in lines:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            pytest.fail(f"non-protocol output on stdout: {line!r}")
        assert message.get("jsonrpc") == "2.0", line
    assert any(m.get("id") == 1 and "result" in m for m in map(json.loads, lines))
