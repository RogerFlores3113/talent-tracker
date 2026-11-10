"""c13 G3: in-chat compare and export wording."""
from __future__ import annotations

import subprocess
from pathlib import Path

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from talent import mcp_server, trust
from talent.mcp_server import build_server

VIEWS = Path(__file__).resolve().parent.parent / "src/talent/static/mcp_views"


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def test_compare_skills_text_closes_and_is_clear(store):
    ids = [store.add_candidate(name=n, tags=t)["id"] for n, t in
           (("Ana", "sql, python, django"), ("Bo", "sql, python"), ("Cy", "sql, go"))]
    t = text(call(build_server(store), "compare_candidates", candidate_ids=ids))
    assert "Skills: Shared: sql. Some of them: python (2 of 3 have it). Only them: django." in t


def test_compare_one_id_message(store):
    a = store.add_candidate(name="Ana")["id"]
    with pytest.raises(Exception) as e:
        call(build_server(store), "compare_candidates", candidate_ids=[a])
    assert "Compare takes two to six people. Name or tick at least two." in str(e.value)


def test_read_only_refusal_is_plain_second_person(store, monkeypatch):
    monkeypatch.setattr(mcp_server, "_client_name", lambda mcp: "Acme Chat")
    a = store.add_candidate(name="Ana Ruiz")["id"]
    s = build_server(store)
    call(s, "search_candidates", query="x")
    trust.set_trust(store, trust.apps(store)[0]["id"], "read_only")
    with pytest.raises(ToolError) as e:
        call(s, "export_candidates_xlsx", candidate_ids=[a])
    msg = str(e.value)
    assert msg.startswith("This AI app is set to read only in Talent Search, so I didn't save a file. You can make it on the Share page: ")
    assert "them" not in msg and "recruiter" not in msg


def test_export_file_names_by_kind(store, tmp_path, monkeypatch):
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(tmp_path / "dl"))
    a = store.add_candidate(name="Ana")["id"]
    s = build_server(store)
    assert call(s, "export_candidates_xlsx", candidate_ids=[a]).structuredContent["file"].startswith("shortlist-")
    assert call(s, "export_candidates_xlsx", candidate_ids=[a], kind="list").structuredContent["file"].startswith("candidates-")


def test_views_wiring():
    assert '"list"' in (VIEWS / "people.js").read_text()
    assert "Load all and export" in (VIEWS / "people.js").read_text()
    assert "have it" in (VIEWS / "compare.js").read_text()


def test_initials_letters_only():
    js = (VIEWS / "kit.js").read_text()
    start = js.index("function initials")
    fn = js[start:js.index("function avatar", start)]
    code = fn + 'console.log(JSON.stringify([initials("=Emily Example"),initials("@@ ++"),initials(""),initials("Ana")]))'
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True)
    if out.returncode != 0 and "not found" in out.stderr + "":
        pytest.skip("node missing")
    assert out.stdout.strip() == '["EE","?","?","A"]'
