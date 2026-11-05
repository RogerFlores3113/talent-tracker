import os
import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from talent.mcp_server import build_server
from talent.mcp_views import URIS


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def structured(result):
    # Typed tools come back as (content, structured); view tools return a CallToolResult.
    if isinstance(result, CallToolResult):
        return result.structuredContent
    content, data = result
    return data.get("result", data)


def text(result):
    content = result.content if isinstance(result, CallToolResult) else result[0]
    return "\n".join(c.text for c in content)


def test_tools_are_listed_and_none_delete(store):
    server = build_server(store)
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    assert {"search_candidates", "save_candidate", "update_candidate", "get_candidate", "log_activity",
            "create_job", "update_job", "edit_job_stages", "add_to_pipeline", "move_stage", "job_pipeline",
            "list_action_items", "add_suggestion", "complete_suggestion", "report_problem"} <= set(tools)
    # Product decision: the AI may never delete a candidate, job or record.
    assert not [n for n in tools if "delete" in n or "remove" in n]
    for name, view in (("get_candidate", "candidate"), ("search_candidates", "search-results"),
                       ("job_pipeline", "pipeline-board"), ("list_action_items", "action-items")):
        meta = tools[name].meta or {}
        assert meta["ui"]["resourceUri"].endswith(view + ".html"), name


def test_ui_resources_are_self_contained(store):
    server = build_server(store)
    resources = anyio.run(server.list_resources)
    assert len(resources) == len(URIS)
    for r in resources:
        assert str(r.uri).startswith("ui://") and r.mimeType == "text/html;profile=mcp-app"
        body = anyio.run(lambda: server.read_resource(r.uri))
        html = list(body)[0].content
        assert "ui/initialize" in html and "ui/notifications/tool-result" in html
        assert "http://" not in html.replace("http://127.0.0.1", "") and "https://" not in html
        assert "innerHTML" not in html


def test_prompts_are_published(store):
    server = build_server(store)
    names = {p.name for p in anyio.run(server.list_prompts)}
    assert {"plan_my_day", "source_candidates", "save_from_linkedin", "save_from_text", "log_recent_emails",
            "log_calendar_interviews", "refresh_stale_profiles"} <= names


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
    res = call(server, "job_pipeline", job_id=job["id"])
    board = structured(res)
    screening = next(s for s in board["stages"] if s["name"] == "Screening")
    assert screening["candidates"][0]["name"] == "Maya"
    assert "Screening (1): Maya" in text(res)  # text fallback for hosts without in-chat views
    detail = structured(call(server, "get_candidate", candidate_id=cid))
    assert detail["pipelines"][0]["stage"] == "Screening"
    assert detail["app_url"].endswith(f"/candidates/{cid}")


def test_custom_stages_logging_and_today(store):
    server = build_server(store)
    cid = structured(call(server, "save_candidate", name="Lee", about="python",
                          work_history=[{"title": "Engineer", "company": "Stripe", "start": "2020"}]))["candidate"]["id"]
    assert structured(call(server, "search_candidates", query="stripe"))["total"] == 1  # past roles are searchable
    job = structured(call(server, "create_job", title="Data Lead", stages=["Applied", "Phone screen", "Onsite", "Hired"]))
    assert job["stages"] == ["Applied", "Phone screen", "Onsite", "Hired", "Rejected", "Withdrawn"]  # Out added
    edited = structured(call(server, "edit_job_stages", job_id=job["id"], rename={"Onsite": "Final round"},
                             add=["Take-home"]))
    assert "Final round" in edited["stages"] and "Take-home" in edited["stages"]
    call(server, "add_to_pipeline", candidate_id=cid, job_id=job["id"])
    with pytest.raises(Exception, match="Phone screen"):
        call(server, "move_stage", candidate_id=cid, job_id=job["id"], stage="teleported")
    logged = structured(call(server, "log_activity", candidate_id=cid, kind="email", summary="Sent intro",
                             when="2026-01-05T10:00:00-05:00"))
    assert logged["last_contacted_at"].startswith("2026-01-05T15:00")
    call(server, "add_suggestion", text="Ping Lee next week", candidate_id=cid)
    today = structured(call(server, "list_action_items"))
    assert any(i["kind"] == "suggestion" for i in today["items"])
    cleared = structured(call(server, "update_candidate", candidate_id=cid, about=""))
    assert "about" not in cleared  # clearing a field is allowed


def test_report_problem_builds_a_link_without_sending(store):
    server = build_server(store)
    out = structured(call(server, "report_problem", summary="Search is slow"))
    assert out["url"].startswith("https://github.com/RogerFlores3113/talent-tracker/issues/new?")


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
