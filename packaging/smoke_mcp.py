"""CI smoke test: launch a built executable as an MCP server over stdio, the way Claude Desktop does.

Usage: python packaging/smoke_mcp.py <path-to-executable>
"""
import os
import sys
import tempfile

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(exe: str) -> None:
    env = {**os.environ, "TALENT_DATA_DIR": tempfile.mkdtemp()}
    params = StdioServerParameters(command=exe, args=["mcp"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            await s.call_tool("save_candidate", {"name": "Smoke Test", "about": "zebra wrangler"})
            res = await s.call_tool("search_candidates", {"query": "zebra"})
            assert not res.isError and res.structuredContent["total"] == 1, res
            views = (await s.list_resources()).resources
            assert len(views) >= 4, views
            html = (await s.read_resource(views[0].uri)).contents[0].text
            assert "ui/initialize" in html
            today = await s.call_tool("list_action_items", {})
            assert not today.isError, today
    print(f"MCP smoke test passed: {exe}")


if __name__ == "__main__":
    anyio.run(main, sys.argv[1])
