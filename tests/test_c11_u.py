"""c11 U: the Pipeline, Finalists, Today, Shortlist and Saved views brought up to the People view standard,
and the People table in full screen. The server side (lean data the views read, complete text replies) runs
everywhere; the browser checks run in the stand-in host (scripts/view_host.py) and skip without Playwright."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import pytest
from mcp.types import CallToolResult

from talent.mcp_server import build_server
from talent.mcp_views import URIS, WITH_TABLE, view_html

ROOT = Path(__file__).resolve().parent.parent


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def data(res):
    return res.structuredContent if isinstance(res, CallToolResult) else res[1].get("result", res[1])


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def _world(store):
    """A job with three people: one in interviews (two sessions with feedback, one coming), one whose interviewer
    still owes feedback, one nobody has met."""
    s = build_server(store)
    job = data(call(s, "create_job", title="Staff Engineer", company="Acme"))["id"]
    ids = [data(call(s, "save_candidate", **p))["id"] for p in (
        {"name": "Maya Ross", "current_title": "Engineer", "current_company": "Ledger"},
        {"name": "Kai Lund", "current_title": "SRE"}, {"name": "Zed Poe"})]
    call(s, "add_to_pipeline", candidate_id=ids[0], job_id=job, stage="Interviewing")
    call(s, "add_to_pipeline", candidate_id=ids[1], job_id=job, stage="Screening")
    call(s, "add_to_pipeline", candidate_id=ids[2], job_id=job)
    return job, *ids


def _seed_interviews(store, s, job, maya, kai):
    past = lambda d: (datetime.now(timezone.utc) - timedelta(days=d)).replace(microsecond=0).isoformat()
    a = data(call(s, "log_activity", candidate_id=maya, kind="interview", summary="Phone screen", when=past(4),
                  job_id=job, interviewer="Priya Shah"))["logged"]["id"]
    b = data(call(s, "log_activity", candidate_id=maya, kind="interview", summary="Coding round", when=past(2),
                  job_id=job, interviewer="Dev Patel"))["logged"]["id"]
    call(s, "log_activity", candidate_id=kai, kind="interview", summary="Screen", when=past(3), job_id=job,
         interviewer="Priya Shah")
    call(s, "add_interview_feedback", interview_id=a, feedback="Clear and kind.", interviewer="Priya Shah",
         verdict="strong yes")
    call(s, "add_interview_feedback", interview_id=b, feedback="Solid Go.", interviewer="Dev Patel", verdict="mixed")


# ------------------------------------------------------------------ the files
def test_every_view_still_clean_and_the_table_ones_load_the_table_css():
    for name in URIS:
        html = view_html(name)
        assert "innerHTML" not in html and "—" not in html and "–" not in html, name
        assert not re.findall(r"""(?:src|href)\s*=\s*['"]?https?://""", html), name
    for name in WITH_TABLE:
        assert "table.ptable" in view_html(name), name
    assert "table.ptable" not in view_html("pipeline")


def test_kit_documents_the_new_helpers():
    from talent.mcp_views import _DIR
    kit = (_DIR / "kit.js").read_text()
    head = kit.split("*/")[0]
    assert "askOrLink(" in head and "role(" in head
    assert "bulkExtra" in kit.split("function firstJob")[0] and "matchLabel" in kit.split("function firstJob")[0]
    assert "compare_finalists" in kit  # Finalists can Refresh


def test_views_never_write_directly():
    from talent.mcp_views import _DIR
    for name in ("pipeline", "decide", "today", "saved"):
        js = (_DIR / f"{name}.js").read_text()
        assert "callTool(" not in js and "tools/call" not in js and "req(" not in js, name
        assert "askOrLink(" in js or "confirmSend(" in js, name  # c11 W: a pick or move asks first, then sends
    # the Shortlist keeps its existing keyed tools and adds no new ones
    js = (_DIR / "shortlist.js").read_text()
    assert "keep_prospects" in js and "pass_prospects" in js and "shortlist_card_key" in js
    assert "move_stage" not in js and "record_offer" not in js


def test_people_table_wraps_instead_of_clipping_and_cuts_only_the_stage():
    css = (ROOT / "src/talent/static/mcp_views/people.css").read_text()
    wide = css.split("@media (min-width:561px){")[1].split("\n}")[0]
    assert "max-width:none" in wide and "white-space:normal" in wide  # cells size to content and wrap
    assert any("k-stage" in ln and "text-overflow:ellipsis" in ln for ln in wide.splitlines())  # only Stage is cut
    kit = (ROOT / "src/talent/static/mcp_views/kit.js").read_text()
    assert 'title:j?stageText(j):null,class:"k-stage' in kit  # ...and its full text is the title


# ------------------------------------------------------------------ Pipeline: the data and the text agree
def test_pipeline_cards_carry_verdicts_owed_and_next_interview(store):
    job, maya, kai, zed = _world(store)
    s = build_server(store)
    _seed_interviews(store, s, job, maya, kai)
    future = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
    call(s, "log_activity", candidate_id=maya, kind="interview", summary="Onsite", when=future, job_id=job,
         interviewer="Sam Roe")
    res = call(s, "job_pipeline", job_id=job)
    d = data(res)
    cards = {c["name"]: c for st in d["stages"] for c in st["candidates"]}
    assert cards["Maya Ross"]["verdicts"] == "Strong yes 1, Mixed 1"
    assert cards["Maya Ross"]["next_interview"] and cards["Maya Ross"]["sessions_done"] == 2
    assert "feedback_owed" not in cards["Maya Ross"]
    assert cards["Kai Lund"]["feedback_owed"] == 1 and cards["Kai Lund"]["owed_by"] == ["Priya Shah"]
    assert "verdicts" not in cards["Zed Poe"] and "feedback_owed" not in cards["Zed Poe"]
    # the header line the view shows is the text's own count line, and the stage counts match the board
    t = text(res)
    assert "2 candidates in process, none hired yet." in t  # the pasted text keeps its own count line
    assert d["headline"].startswith("3 in the pipeline. 2 count in the status update (Sourced is left out).")
    for st in d["stages"]:
        assert len(st["candidates"]) == sum(1 for c in cards.values() if c in st["candidates"])
    # the verdicts and what is owed are in the text too, for the recruiter only, never in the pasted update
    first, *rest = res.content
    assert "Strong yes" not in first.text and "Priya Shah" not in first.text
    assert any("interview feedback so far" in c.text and "Maya Ross (Strong yes 1, Mixed 1)" in c.text
               and "feedback still owed by Priya Shah" in c.text for c in rest)


def test_pipeline_text_has_no_feedback_line_without_interviews(store):
    job, *_ = _world(store)
    res = call(build_server(store), "job_pipeline", job_id=job)
    assert "interview feedback so far" not in text(res)


# ------------------------------------------------------------------ Finalists and Today
def test_finalists_data_has_what_the_table_needs(store):
    job, maya, kai, zed = _world(store)
    s = build_server(store)
    _seed_interviews(store, s, job, maya, kai)
    d = data(call(s, "compare_finalists", job_id=job))
    by = {p["name"]: p for p in d["people"]}
    maya_row = by["Maya Ross"]
    assert maya_row["tally"] == "Strong yes 1, Mixed 1" and maya_row["stage"] == "Interviewing"
    assert [i["feedback"][0]["text"] for i in maya_row["interviews"]] == ["Clear and kind.", "Solid Go."]
    assert by["Kai Lund"]["owed"] == ["Priya Shah"]
    assert d["out_stage"] and "app_url" in d and d["job"]["id"] == job


def test_today_rows_name_the_person_and_the_job(store):
    job, maya, kai, zed = _world(store)
    s = build_server(store)
    _seed_interviews(store, s, job, maya, kai)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
    call(s, "log_activity", candidate_id=zed, kind="interview", summary="Intro", when=future, job_id=job,
         interviewer="Sam Roe")
    d = data(call(s, "list_action_items"))
    rows = [i for i in d["items"] if i.get("candidate_id")]
    assert rows and all(i["name"] and i["job"] == "Staff Engineer, Acme" for i in rows if i.get("job_id"))
    assert any(i["name"] == "Zed Poe" and i["kind"] == "interview" for i in rows)
    assert "Zed Poe" in text(call(s, "list_action_items"))  # the text stays complete on its own


# ------------------------------------------------------------------ Saved batch
def test_saved_rows_carry_facts_and_a_no_contact_flag_in_data_and_text(store):
    s = build_server(store)
    job = data(call(s, "create_job", title="Staff Engineer", company="Acme"))["id"]
    res = call(s, "save_candidates", job_id=job, people=[
        {"name": "Ada Lin", "title": "CTO", "company": "Foo", "location": "Paris", "email": "ada@foo.com",
         "links": ["https://github.com/adalin"]},
        {"name": "Quiet Person", "company": "Nowhere"}])
    d = data(res)
    ada, quiet = d["saved"]
    assert ada["current_title"] == "CTO" and ada["location"] == "Paris" and "no_contact" not in ada
    assert ada["links"] == ["https://github.com/adalin"]
    assert quiet["no_contact"] is True and quiet["current_company"] == "Nowhere"
    t = text(res)
    assert "Ada Lin" in t and "CTO at Foo, Paris" in t
    assert "Quiet Person" in t and "No email or LinkedIn saved" in t
    assert t.count("No email or LinkedIn saved") == 1


def test_needs_answer_lists_the_actual_people_not_a_count(store):
    s = build_server(store)
    job = data(call(s, "create_job", title="Staff Engineer", company="Acme"))["id"]
    for i in range(7):
        store.add_candidate(name="Sam Rivera", current_title="Designer", current_company=f"Co{i}", location="Austin")
    res = call(s, "save_candidates", job_id=job, people=[{"name": "Sam Rivera", "title": "PM", "company": "New"}])
    d = data(res)
    row = d["needs_answer"][0]
    assert len(row["possible_duplicates"]) == 7 and "more_duplicates" not in row
    assert {m["current_company"] for m in row["possible_duplicates"]} == {f"Co{i}" for i in range(7)}
    assert all(m["id"] and m["app_url"] for m in row["possible_duplicates"])
    t = text(res)
    assert t.count("  - Could be Sam Rivera (id ") == 7 and "Designer at Co6, Austin" in t
    assert d["counts"]["saved"] == 0
