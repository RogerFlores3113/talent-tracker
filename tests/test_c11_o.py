"""Cycle 11 builder O: leftovers from cycle 10. Search result cards with the name as the link, a cap on interview
notes from the AI app, feedback from someone not on a session, working hours and start times in the loop planner,
and "Add tag" / "Move stage" on the Candidates bulk bar."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from conftest import flash_of, local_client
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from talent import interviews, loops, reports
from talent.mcp_server import build_server
from talent.store import Store, set_shown_zone


@pytest.fixture(autouse=True)
def _zone():
    yield
    set_shown_zone(None)


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def data(res):
    return res.structuredContent if isinstance(res, CallToolResult) else res[1]


def _world(store, name="Priya Raman"):
    job = store.add_job(title="Staff Engineer", company="Acme")
    cid = store.add_candidate(name=name, email="p@example.com")["id"]
    store.add_to_pipeline(cid, job["id"], "Interviews")
    return job["id"], cid


# ------------------------------------------------------------------ 1. result cards
def test_search_result_name_is_the_link_and_inner_links_are_their_own(store):
    c = store.add_candidate(name="Ana Ray", current_title="Engineer", tags="python",
                            linkedin_url="https://linkedin.com/in/anaray", website="https://ana.example.com")
    html = local_client(store).get("/search", params={"q": "python"}).text
    assert f'<a class="card-name" href="/candidates/{c["id"]}">Ana Ray</a>' in html
    assert '<div class="card card-linked' in html and '<a class="card "' not in html and '<a class="card"' not in html
    assert 'href="https://linkedin.com/in/anaray"' in html and 'href="https://ana.example.com"' in html
    assert html.index("card-name") < html.index("linkedin.com/in/anaray")  # name first in tab order
    # no link sits inside another link
    for card in re.findall(r'<div class="card card-linked.*?</div>\s*</li>', html, re.S):
        assert not re.search(r"<a [^>]*>(?:(?!</a>).)*<a ", card, re.S)


# ------------------------------------------------------------------ 2. note length cap on MCP
def test_interview_note_over_500_characters_is_refused_with_the_length(store):
    jid, cid = _world(store)
    s = build_server(store)
    long = "x" * 501
    with pytest.raises(ToolError, match="up to 500 characters, and this one has 501"):
        call(s, "log_activity", candidate_id=cid, kind="interview", summary=long, when="2026-12-12T10:00:00-08:00",
             job_id=jid)
    assert store.timeline(cid) == [] or all(t["kind"] != "interview" for t in store.timeline(cid))
    ok = call(s, "log_activity", candidate_id=cid, kind="interview", summary="x" * 500,
              when="2026-12-12T10:00:00-08:00", job_id=jid)
    iid = data(ok)["logged"]["id"]
    with pytest.raises(ToolError, match="up to 500 characters, and this one has 600"):
        call(s, "update_interview", interview_id=iid, outcome="yes", note="y" * 600)
    with pytest.raises(ToolError, match="interview note"):
        call(s, "update_interview", interview_id=iid, note="y" * 600)


def test_a_long_note_on_other_kinds_is_not_capped_at_500(store):
    jid, cid = _world(store)
    s = build_server(store)
    call(s, "log_activity", candidate_id=cid, kind="note", summary="n" * 800)


def test_feedback_keeps_its_own_longer_limit(store):
    jid, cid = _world(store)
    s = build_server(store)
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    iid = data(call(s, "log_activity", candidate_id=cid, kind="interview", summary="Screen", when=past,
                    job_id=jid, interviewer="Sam Roe"))["logged"]["id"]
    call(s, "add_interview_feedback", interview_id=iid, feedback="f" * 900, interviewer="Sam Roe")
    with pytest.raises(ToolError, match="too long"):
        call(s, "add_interview_feedback", interview_id=iid, feedback="f" * 4001, interviewer="Sam Roe")


# ------------------------------------------------------------------ 3. unlisted feedback
def _past_session(store, who="Sam Roe"):
    jid, cid = _world(store)
    s = build_server(store)
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    iid = data(call(s, "log_activity", candidate_id=cid, kind="interview", summary="Screen", when=past,
                    job_id=jid, interviewer=who))["logged"]["id"]
    return s, jid, cid, iid


def test_an_unlisted_verdict_is_kept_but_does_not_decide_the_result(store):
    s, jid, cid, iid = _past_session(store)
    res = call(s, "add_interview_feedback", interview_id=iid, feedback="Loved them", interviewer="Pat Out",
               verdict="strong yes")
    assert "not counted in the session's result" in text(res)
    iv = store.get_interview(iid)
    assert any(f["by"] == "Pat Out" for f in iv["data"]["feedback"])
    assert interview_result(iv) is None  # still no outcome
    call(s, "add_interview_feedback", interview_id=iid, feedback="Mixed bag", interviewer="Sam Roe", verdict="no")
    iv = store.get_interview(iid)
    assert iv["data"]["verdict"] == "no"  # Sam alone decides; Pat's strong yes is not in the roll-up
    assert [v["by"] for v in interviews.verdicts(iv).values()] == ["Sam Roe"]


def interview_result(iv):
    from talent.store import interview_label
    return interview_label(iv["data"])


def test_unlisted_feedback_is_labelled_in_the_web_and_in_decide_text(store):
    s, jid, cid, iid = _past_session(store)
    call(s, "add_interview_feedback", interview_id=iid, feedback="Seemed sharp", interviewer="Pat Out")
    call(s, "add_interview_feedback", interview_id=iid, feedback="Fine", interviewer="Sam Roe")
    page = local_client(store).get(f"/candidates/{cid}").text
    assert "Pat Out (from someone not on this interview, not counted in the result)" in page
    assert "Sam Roe (from someone" not in page
    from talent import decide
    d = decide.build(store, jid, [cid]) if hasattr(decide, "build") else None
    html = local_client(store).get(f"/jobs/{jid}/decide?ids={cid}").text
    assert "from someone not on this interview" in html
    t = text(call(s, "get_candidate", candidate_id=cid))
    assert "Pat Out (from someone not on this interview, not counted in the result)" in t
    assert "Sam Roe (from someone" not in t


def test_a_session_with_nobody_listed_counts_everyone(store):
    jid, cid = _world(store)
    s = build_server(store)
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    iid = data(call(s, "log_activity", candidate_id=cid, kind="interview", summary="Screen", when=past,
                    job_id=jid))["logged"]["id"]
    call(s, "add_interview_feedback", interview_id=iid, feedback="Good", interviewer="Pat Out", verdict="yes")
    assert store.get_interview(iid)["data"]["verdict"] == "yes"


# ------------------------------------------------------------------ 4. loop planner
def _plan(store, jid, cid, start, **kw):
    return loops.plan(store, jid, cid, start=start, now=datetime(2026, 11, 2, tzinfo=timezone.utc), **kw)


def test_start_with_a_time_means_not_before_that_time(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    jid, cid = _world(store)
    loops.save_template(store, jid, "Screen | 60", "9-17")
    p = _plan(store, jid, cid, "2026-11-12T13:00")
    assert p["sessions"][0]["at"] == "2026-11-12T21:00:00+00:00"  # 1 PM Pacific
    p = _plan(store, jid, cid, "2026-11-12")
    assert p["sessions"][0]["at"] == "2026-11-12T17:00:00+00:00"  # 9 AM Pacific
    with pytest.raises(ValueError, match="start is a day like"):
        _plan(store, jid, cid, "2026-11-12Tlunch")


def test_a_late_start_time_moves_to_the_next_day(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    jid, cid = _world(store)
    loops.save_template(store, jid, "Screen | 60", "9-17")
    p = _plan(store, jid, cid, "2026-11-12T16:30")
    assert p["sessions"][0]["at"] == "2026-11-13T17:00:00+00:00"


def test_an_interviewers_own_hours_are_honoured(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    jid, cid = _world(store)
    loops.save_template(store, jid, "Screen | 60 | Dana Lee", "8-18")
    store.save_interviewer("Dana Lee", None, "America/Los_Angeles")
    p = _plan(store, jid, cid, "2026-11-12")
    assert p["sessions"][0]["at"] == "2026-11-12T17:00:00+00:00"  # default 9 AM, not 8 AM
    loops.save_person_hours(store, "Dana Lee", "11-15")
    p = _plan(store, jid, cid, "2026-11-12")
    assert p["sessions"][0]["at"] == "2026-11-12T19:00:00+00:00"  # 11 AM
    loops.save_person_hours(store, "Dana Lee", "6-7")
    p = _plan(store, jid, cid, "2026-11-12")
    assert not p["sessions"][0]["placed"]
    assert loops.save_person_hours(store, "Dana Lee", "") is None
    assert _plan(store, jid, cid, "2026-11-12")["sessions"][0]["placed"]


def test_hours_in_another_zone_use_that_zone(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    jid, cid = _world(store)
    loops.save_template(store, jid, "Screen | 60 | Dana Lee", "6-20")
    store.save_interviewer("Dana Lee", None, "America/New_York")
    loops.save_person_hours(store, "Dana", "13-17")  # 1 to 5 PM in New York
    p = _plan(store, jid, cid, "2026-11-12")
    assert p["sessions"][0]["at"] == "2026-11-12T18:00:00+00:00"  # 1 PM New York
    assert loops.person_hours(loops.person_hours_book(store), "Dana Lee") == (13, 17)


def test_bad_hours_are_refused(store):
    with pytest.raises(ValueError, match="Working hours look like"):
        loops.save_person_hours(store, "Dana Lee", "late")


def test_the_log_form_saves_working_hours_with_the_interviewer(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    jid, cid = _world(store)
    c = local_client(store)
    base = {"kind": "interview", "body": "Screen", "at": "2026-11-20T12:00", "tz": "America/Los_Angeles",
            "job_id": str(jid), "interviewer": "Dana Lee"}
    bad = c.post(f"/candidates/{cid}/log", data={**base, "interviewer_hours": "late"}, follow_redirects=False)
    assert "working hours look like 9-17" in flash_of(bad, "flash_error") or "working hours" in flash_of(bad, "error")
    r = c.post(f"/candidates/{cid}/log", data={**base, "interviewer_hours": "10-15"}, follow_redirects=False)
    assert r.status_code == 303
    assert loops.person_hours(loops.person_hours_book(store), "Dana Lee") == (10, 15)
    assert 'name="interviewer_hours"' in c.get(f"/candidates/{cid}").text


def test_the_loop_page_takes_a_not_before_time(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    jid, cid = _world(store)
    loops.save_template(store, jid, "Screen | 60", "9-17")
    html = local_client(store).get(f"/jobs/{jid}/loop", params={
        "candidate": cid, "start": "2099-01-12", "start_time": "14:00"}).text
    assert 'name="start_time"' in html and "2 PM" in html or "2:00 PM" in html


def test_plan_loop_over_mcp_reads_the_time(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    jid, cid = _world(store)
    loops.save_template(store, jid, "Screen | 60", "9-17")
    s = build_server(store)
    res = call(s, "plan_loop", job_id=jid, candidate_id=cid, start="2099-01-12T13:00")
    assert data(res)["sessions"][0]["at"].startswith("2099-01-12T21:00")


# ------------------------------------------------------------------ 5. bulk bar
def _two_jobs(store):
    a = store.add_job(title="Backend", company="Acme")["id"]
    b = store.add_job(title="Design", company="Acme")["id"]
    ids = [store.add_candidate(name=n, tags="go")["id"] for n in ("Ann One", "Bob Two", "Cy Three")]
    store.add_to_pipeline(ids[0], a, "Contacted")
    store.add_to_pipeline(ids[1], a, "Contacted")
    store.add_to_pipeline(ids[2], b, "Contacted")
    return a, b, ids


def test_the_bulk_bar_has_add_tag_and_move_stage(store):
    _two_jobs(store)
    html = local_client(store).get("/candidates").text
    assert 'name="action" value="tag"' in html and 'name="action" value="move"' in html and 'name="tag"' in html


def test_bulk_add_tag_keeps_old_tags_and_skips_repeats(store):
    a, b, ids = _two_jobs(store)
    c = local_client(store)
    r = c.post("/candidates/bulk", data={"action": "tag", "tag": "Python, go", "candidate_ids": [str(i) for i in ids]},
               follow_redirects=False)
    assert r.status_code == 303
    note = flash_of(r, "notice")
    assert "3 people" in note
    for i in ids:
        assert [t.lower() for t in store.get_candidate(i)["tags"]] == ["go", "python"]
    again = c.post("/candidates/bulk", data={"action": "tag", "tag": "python", "candidate_ids": [str(ids[0])]},
                   follow_redirects=False)
    assert "Added python to 0 people. 1 already had it" in flash_of(again, "notice")
    none = c.post("/candidates/bulk", data={"action": "tag", "tag": " ", "candidate_ids": [str(ids[0])]},
                  follow_redirects=False)
    assert "Type the tag" in flash_of(none, "flash_error") or "Type the tag" in flash_of(none, "error")


def test_bulk_move_stage_moves_people_in_the_job_and_skips_the_rest(store):
    a, b, ids = _two_jobs(store)
    stage = store.job_stages(a)[2]["name"]
    c = local_client(store)
    r = c.post("/candidates/bulk", data={"action": "move", "job_id": str(a), "stage": stage,
                                         "candidate_ids": [str(i) for i in ids]}, follow_redirects=False)
    note = flash_of(r, "notice")
    assert f"Moved 2 people to {stage}" in note and "Skipped 1 person who is not in this job" in note
    rows = {x["candidate_id"]: x["stage"] for x in store._rows("SELECT candidate_id, stage FROM applications WHERE job_id = ?", (a,))}
    assert rows == {ids[0]: stage, ids[1]: stage}
    assert store._row("SELECT stage FROM applications WHERE candidate_id = ?", (ids[2],))["stage"] == "Contacted"
    again = c.post("/candidates/bulk", data={"action": "move", "job_id": str(a), "stage": stage,
                                             "candidate_ids": [str(ids[0])]}, follow_redirects=False)
    assert "already there" in flash_of(again, "notice")


def test_bulk_move_needs_a_job_and_a_real_stage(store):
    a, b, ids = _two_jobs(store)
    c = local_client(store)
    r = c.post("/candidates/bulk", data={"action": "move", "stage": "Hired-ish", "candidate_ids": [str(ids[0])]},
               follow_redirects=False)
    assert "Pick the job" in (flash_of(r, "flash_error") or flash_of(r, "error"))
    r = c.post("/candidates/bulk", data={"action": "move", "job_id": str(a), "stage": "Nonsense Stage",
                                         "candidate_ids": [str(ids[0])]}, follow_redirects=False)
    assert "Nobody was moved" in (flash_of(r, "flash_error") or flash_of(r, "error"))
    r = c.post("/candidates/bulk", data={"action": "move", "job_id": str(a), "stage": "",
                                         "candidate_ids": [str(ids[0])]}, follow_redirects=False)
    assert "Type the stage" in (flash_of(r, "flash_error") or flash_of(r, "error"))


def test_there_is_no_mcp_tool_for_bulk_tag_or_move(store):
    names = {t.name for t in anyio.run(build_server(store).list_tools)}
    assert not {n for n in names if "bulk" in n}
