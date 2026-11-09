"""Cycle 10 glue: the AI app's replies pass on what the time reader learned (a time that happens twice)."""
from __future__ import annotations

import anyio
import pytest
from mcp.types import CallToolResult

from talent.mcp_server import build_server
from talent.store import Store


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def data(res):
    return res.structuredContent if isinstance(res, CallToolResult) else res[1]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "talent.db")
    yield s
    s.close()


def test_log_activity_says_when_a_fall_back_time_happens_twice(store):
    s = build_server(store)
    cid = data(call(s, "save_candidate", name="Ana Ruiz", email="ana@example.com"))["id"]
    res = call(s, "log_activity", candidate_id=cid, kind="interview", summary="Screen (30 min)",
               when="2027-11-07 1:30 AM America/New_York")
    assert "happens twice" in text(res)
    assert any("happens twice" in n for n in data(res)["when_notes"])


def test_update_interview_says_when_a_fall_back_time_happens_twice(store):
    s = build_server(store)
    cid = data(call(s, "save_candidate", name="Ana Ruiz", email="ana@example.com"))["id"]
    res = call(s, "log_activity", candidate_id=cid, kind="interview", summary="Screen (30 min)",
               when="2027-11-01 10:00 AM America/New_York")
    iid = data(res)["logged"]["id"]
    res = call(s, "update_interview", interview_id=iid, when="2027-11-07 1:30 AM America/New_York")
    assert "happens twice" in text(res)


# ---------------------------------------------------------------- Claude Code connect command
def test_claude_code_command_adds_our_server_for_every_project():
    from talent.integrations import LaunchSpec, claude_code_command
    spec = LaunchSpec("/Applications/Talent Search.app/Contents/MacOS/talent-search", ["mcp"],
                      {"TALENT_DATA_DIR": "/Users/ana/Library/Application Support/Talent Search"})
    cmd = claude_code_command(spec, windows=False)
    assert cmd.startswith("claude mcp add --transport stdio --scope user --env ")
    assert "'TALENT_DATA_DIR=/Users/ana/Library/Application Support/Talent Search'" in cmd
    assert cmd.endswith("talent-search -- '/Applications/Talent Search.app/Contents/MacOS/talent-search' mcp")
    win = claude_code_command(LaunchSpec(r"C:\Program Files\Talent Search\talent-search-mcp.exe", ["mcp"],
                                         {"TALENT_DATA_DIR": r"C:\Users\Ana\AppData\Local\Talent Search"}),
                              windows=True)
    assert '"C:\\Program Files\\Talent Search\\talent-search-mcp.exe" mcp' in win


def test_connect_page_shows_the_claude_code_command(store):
    from conftest import local_client
    html = local_client(store).get("/connect").text
    assert 'id="claude-code-cmd"' in html and "claude mcp add --transport stdio --scope user" in html
    assert 'data-copy-from="claude-code-cmd"' in html
