import os
import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from talent.mcp_server import build_server


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def structured(result):
    # FastMCP returns (content, structured) for typed tools
    content, data = result
    return data.get("result", data)


def test_tools_are_listed(store):
    server = build_server(store)
    names = {t.name for t in anyio.run(server.list_tools)}
    assert {"search_candidates", "save_candidate", "update_candidate", "get_candidate",
            "delete_candidate", "create_job", "add_to_pipeline", "move_stage", "job_pipeline"} <= names


def test_save_search_merge_and_pipeline(store):
    server = build_server(store)
    saved = structured(call(server, "save_candidate", name="Maya", email="maya@x.com",
                            about="Go and Rust payments engineer", location="Austin, TX", tags=["go", "rust"]))
    assert saved["created"] is True
    cid = saved["candidate"]["id"]
    merged = structured(call(server, "save_candidate", name="Maya", email="MAYA@x.com", current_title="Staff Engineer"))
    assert merged["created"] is False and merged["candidate"]["about"].startswith("Go and Rust")

    found = structured(call(server, "search_candidates", query="rust"))
    assert found["total"] == 1 and "**Rust**" in found["results"][0]["match"]
    assert "phone" not in found["results"][0]  # empty fields trimmed

    job = structured(call(server, "create_job", title="Payments Lead"))
    call(server, "add_to_pipeline", candidate_id=cid, job_id=job["id"])
    call(server, "move_stage", candidate_id=cid, job_id=job["id"], stage="screening", notes="Strong")
    board = structured(call(server, "job_pipeline", job_id=job["id"]))
    assert board["stages"]["screening"][0]["name"] == "Maya"
    detail = structured(call(server, "get_candidate", candidate_id=cid))
    assert detail["pipelines"][0]["stage"] == "screening"


def test_errors_are_plain_english(store):
    server = build_server(store)
    with pytest.raises(Exception, match="No candidate with id 999"):
        call(server, "get_candidate", candidate_id=999)


def test_stdio_end_to_end(tmp_path):
    """Launch the server the way Claude Desktop does and talk MCP to it."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "talent", "mcp"],
        env={**os.environ, "TALENT_DATA_DIR": str(tmp_path / "d")},
    )

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert any(t.name == "save_candidate" for t in tools.tools)
                await session.call_tool("save_candidate", {"name": "Stdio Sam", "about": "elixir"})
                res = await session.call_tool("search_candidates", {"query": "elixir"})
                assert not res.isError
                assert res.structuredContent["total"] == 1

    anyio.run(go)
