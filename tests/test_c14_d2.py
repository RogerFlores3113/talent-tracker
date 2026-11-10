"""Cycle 14 D2: the in-chat views catch up with the web (highlight argument, table columns, compare, exports)."""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from conftest import local_client
from talent import mcp_server
from talent.mcp_server import build_server
from talent.mcp_views import view_html

ROOT = Path(__file__).resolve().parent.parent
VIEWS = ROOT / "src" / "talent" / "static" / "mcp_views"


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def data(res):
    return res.structuredContent if isinstance(res, CallToolResult) else res[1]


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def people(store):
    rows = [dict(name="Ana Ruiz", tags="python, sql", title="Data Engineer"),
            dict(name="Ben Cho", tags="python", title="Backend Developer"),
            dict(name="Cy Dunn", tags="go", title="Data Engineer"),
            dict(name="Di Eze", tags="python, sql, go", title="Staff Engineer")]
    out = []
    for r in rows:
        out.append(store.add_candidate(name=r["name"], tags=r["tags"], current_title=r["title"])["id"])
    return out


# ---------------------------------------------------------------- 1. the highlight argument
def test_highlight_helpers_map_to_the_shared_format():
    picks = mcp_server._highlight_picks(["python", "title:Data Engineer", "Python", "sql:blue"])
    assert [(p["name"], p["color"], p["kind"]) for p in picks] == [
        ("python", "red", "skill"), ("data engineer", "yellow", "title"), ("sql", "blue", "skill")]
    assert mcp_server._hl_string(picks) == "python:red,title:data engineer:yellow,sql:blue"
    assert mcp_server._highlight_picks(None) == [] and mcp_server._highlight_picks([]) == []
    assert len(mcp_server._highlight_picks([f"s{i}" for i in range(9)])) == 6


def test_search_highlight_tiers_and_echoes(store):
    ana, ben, cy, di = people(store)
    s = build_server(store)
    res = call(s, "search_candidates", query="", highlight=["python", "title:Data Engineer"])
    d = data(res)
    names = [r["name"] for r in d["results"]]
    assert names[0] == "Ana Ruiz"  # python and the title: both picks
    assert names.index("Di Eze") < names.index("Cy Dunn") or names.index("Ben Cho") < names.index("Cy Dunn")
    assert d["hl"] == "python:red,title:data engineer:yellow"
    assert d["highlight"][1] == {"name": "data engineer", "color": "yellow", "kind": "title"}
    assert "hl=python%3Ared" in d["app_url"] and "Highlighted: python (red), title: data engineer (yellow)" in text(res)
    assert "highlight" not in data(call(s, "search_candidates", query=""))  # nothing echoed when nothing is picked


def test_highlight_aliases_and_refusals_stay(store):
    people(store)
    s = build_server(store)
    d = data(call(s, "search_candidates", query="", highlight_skills=["python"]))
    assert d["highlight"][0]["name"] == "python"
    with pytest.raises(ToolError) as e:
        call(s, "search_candidates", query="", color_picks=["x"])
    assert "doesn't take color_picks" in str(e.value)
    assert "highlight_skills" in mcp_server.ARG_ALIASES["compare_candidates"]
    assert "highlight_skills" in mcp_server.ARG_ALIASES["run_saved_search"]


def test_compare_highlight_links_and_order(store):
    ana, ben, cy, di = people(store)
    s = build_server(store)
    res = call(s, "compare_candidates", candidate_ids=[di, ana, cy], highlight=["python"])
    d = data(res)
    assert [p["id"] for p in d["people"]] == [di, ana, cy]  # the order given, as the web keeps it
    assert d["hl"] == "python:red" and "view=hm" in d["hm_url"] and "hl=python:red" in d["hm_url"]
    assert "contact=1" in d["for_me_url"] and "notes=1" in d["for_me_url"]
    # the web page lists the same people in the same order
    c = local_client(store)
    html = c.get(f"/candidates/compare?ids={di},{ana},{cy}&hl=python:red").text
    order = [html.index(n) for n in ("Di Eze", "Ana Ruiz", "Cy Dunn")]
    assert order == sorted(order)


def test_compare_names_who_was_left_out_first(store):
    ids = [store.add_candidate(name=f"Person {i}")["id"] for i in range(8)]
    s = build_server(store)
    t = text(call(s, "compare_candidates", candidate_ids=ids))
    lines = t.splitlines()
    left = next(i for i, ln in enumerate(lines) if ln.startswith("Left out, because compare takes 6 at a time"))
    assert left < next(i for i, ln in enumerate(lines) if ln.startswith("- Person"))
    assert len(data(call(s, "compare_candidates", candidate_ids=ids))["left_out"]) == 2


# ---------------------------------------------------------------- 5. exports
@pytest.fixture
def downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(tmp_path / "dl"))


def test_export_with_only_a_job_exports_that_jobs_people(store, downloads):
    ana, ben, cy, di = people(store)
    job = store.add_job("Data Lead", company="Acme")["id"]
    store.add_to_pipeline(ana, job, "Sourced")
    store.add_to_pipeline(ben, job, "Sourced")
    store.add_to_pipeline(cy, job, "Rejected")  # people who are out stay out of the file
    s = build_server(store)
    res = call(s, "export_candidates_xlsx", job_id=job)
    d = data(res)
    assert d["written"] and d["people"] == 2 and Path(d["path"]).exists()
    assert d["path"] in text(res)  # the full path, not only the folder
    assert "contact=1" in d["for_me_url"] and "notes=1" in d["for_me_url"]
    with pytest.raises((ValueError, ToolError)) as e:
        call(s, "export_candidates_xlsx")
    assert "candidate_ids" in str(e.value) and "job_id" in str(e.value)


# ---------------------------------------------------------------- views (source)
def test_people_view_has_columns_picks_and_exports():
    html = view_html("people")
    kit = (VIEWS / "kit.js").read_text()
    # column order: Name, Title, Skills, Job, Stage, then the rest; four go behind Columns
    order = re.findall(r'\{k:"(\w+)",l:', kit.split("const TCOLS=[")[1].split("];")[0])
    assert order[:5] == ["name", "title", "skills", "job", "stage"]
    for k in ("company", "location", "updated", "added"):
        assert f'{{k:"{k}"' in kit and "opt:1" in kit.split(f'{{k:"{k}"')[1].split("}")[0] + "}"
    assert '"Columns"' in kit and "st.cols" in kit
    # screen reader names
    assert '", highlighted "+h.color+", pick "+(i+1)+" (remove)"' in kit
    assert '"Remove highlight: "+hlText(x)+" ("+x.color+")"' in kit
    assert '"title: "+titleCase(' in kit and "Top match: " in kit and "in the top tier" in kit
    # no forced fullscreen; chips in cards; picks kept in the tool arguments
    people = (VIEWS / "people.js").read_text()
    assert 'setMode("fullscreen")' not in people
    assert "onSkill:cardPick" in people and "ARGS=a" in people and "a.highlight=h" in people
    assert "Export for hiring manager" in people and "Export for me" in people
    assert "contact=1&notes=1" in people
    assert "position:sticky" in view_html("people").split(".hlbar{")[1].split("}")[0]
    assert "innerHTML" not in kit and "innerHTML" not in people
    assert "tr.sel>th[scope=row]" in html


def test_compare_view_has_hm_button_colors_and_fits():
    js = (VIEWS / "compare.js").read_text()
    for needle in ("Hiring manager view", "d.hm_url", "Export for hiring manager", "Export for me", "d.for_me_url",
                   "cmpChip(", "cmpPicks("):
        assert needle in js, needle
    css = (VIEWS / "compare.css").read_text()
    assert "9.5rem" in css and "white-space:nowrap" in css
    assert "chip[data-hl=red]" in view_html("compare")  # colors reach the stand-alone compare view


def test_kit_reports_height_from_content_and_pipeline_has_a_legend():
    kit = (VIEWS / "kit.js").read_text()
    assert "function viewHeight()" in kit and "getBoundingClientRect().bottom" in kit
    assert "A star means" in (VIEWS / "pipeline.js").read_text()


