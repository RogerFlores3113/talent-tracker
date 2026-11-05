"""Cycle 4 fixer Q: interviews, time and hiring managers."""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from conftest import flash_of, local_client
from talent import actions
from talent.mcp_server import build_server
from talent.store import parse_when, to_dt
from talent.web import _ago

from test_mcp import call, structured, text


def _at(days=2, hours=0):
    return (datetime.now(timezone.utc) + timedelta(days=days, hours=hours)).replace(second=0, microsecond=0)


@pytest.fixture
def chicago(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset isn't available on Windows")
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def utc_clock(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset isn't available on Windows")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _dana(store, stages=None, openings=None):
    job = store.add_job("Backend Engineer", company="Acme", stages=stages,
                        **({"openings": openings} if openings else {}))
    dana = store.add_candidate(name="Dana Okafor")["id"]
    store.add_to_pipeline(dana, job["id"], "Screening")
    return job, dana


# ------------------------------------------------- 1. out but still booked
def test_interview_after_a_move_out_stays_on_today_with_cancel_and_keep(store):
    job, dana = _dana(store)
    iv = store.log(dana, "interview", "Phone screen", at=_at(1), job_id=job["id"])
    store.move_stage(dana, job["id"], "Rejected")
    day = actions.local_time(_at(1).isoformat(), "%a %b %-d")
    items = actions.action_items(store)["items"]
    row = next(i for i in items if i["kind"] == "interview_out")
    assert row["title"] == f"Dana Okafor is out of Backend Engineer but still has an interview {day}"
    assert row["interview_ids"] == [iv["id"]] and not [i for i in items if i["kind"] == "interview"]
    out = text(call(build_server(store), "list_action_items"))
    assert "is out of Backend Engineer but still has an interview" in out and f"interview_ids {iv['id']}" in out
    c = local_client(store)
    home = c.get("/").text
    assert "Cancel interview" in home and "Keep it" in home and "/actions/interviews/out" in home
    r = c.post("/actions/interviews/out", data={"candidate_id": dana, "job_id": job["id"], "op": "keep"},
               headers={"referer": "http://127.0.0.1:8765/"}, follow_redirects=False)
    assert "Kept the interview" in flash_of(r)
    assert store.get_interview(iv["id"])["data"].get("kept_at")
    items = actions.action_items(store)["items"]
    assert not [i for i in items if i["kind"] == "interview_out"]
    assert [i["interview_id"] for i in items if i["kind"] == "interview"] == [iv["id"]]  # a normal interview now


def test_cancel_from_today_and_far_off_and_hired_interviews(store):
    job, dana = _dana(store)
    far = store.log(dana, "interview", "Final", at=_at(30), job_id=job["id"])
    store.move_stage(dana, job["id"], "Hired")
    row = next(i for i in actions.action_items(store)["items"] if i["kind"] == "interview_out")
    assert row["title"].startswith("Dana Okafor was hired for Backend Engineer but still has an interview")
    c = local_client(store)
    r = c.post("/actions/interviews/out", data={"candidate_id": dana, "job_id": job["id"], "op": "cancel"},
               follow_redirects=False)
    assert "Cancelled the interview" in flash_of(r) and store.get_interview(far["id"])["status"] == "cancelled"
    assert not [i for i in actions.action_items(store)["items"] if i["kind"] == "interview_out"]
    r = c.post("/actions/interviews/out", data={"candidate_id": dana, "job_id": job["id"], "op": "cancel"},
               follow_redirects=False)
    assert "Nothing to change" in flash_of(r)


def test_a_loop_of_interviews_after_a_move_out_is_one_row(store):
    job, dana = _dana(store)
    for h in (0, 1, 2):
        store.log(dana, "interview", f"Onsite {h}", at=_at(3, h), job_id=job["id"])
    store.move_stage(dana, job["id"], "Withdrawn")
    rows = [i for i in actions.action_items(store)["items"] if i["kind"] == "interview_out"]
    assert len(rows) == 1 and len(rows[0]["interview_ids"]) == 3 and "3 interviews (" in rows[0]["title"]


# ------------------------------------------------------------ 2. loops
def test_onsite_loop_is_not_a_replacement_and_names_each_session(store):
    job, dana = _dana(store)
    server = build_server(store)
    day = _at(3).replace(hour=15, minute=0)
    outs = [structured(call(server, "log_activity", candidate_id=dana, kind="interview", summary=s,
                            when=(day + timedelta(hours=h)).isoformat(), job_id=job["id"]))
            for h, s in enumerate(("Onsite: system design", "Onsite: coding", "Onsite: values"))]
    assert all("warning" not in o for o in outs) and "one loop" in outs[2]["note"]
    status = actions.job_status_text(store, job["id"])
    assert "3 interviews:" in status and "Onsite: system design" in status and "Onsite: values" in status
    other = structured(call(server, "log_activity", candidate_id=dana, kind="interview", summary="Debrief",
                            when=(day + timedelta(days=2)).isoformat(), job_id=job["id"]))
    assert "replaces it" in other["warning"]


# ------------------------------------------------------- 3. time zones
def test_parse_when_reads_zone_names_and_us_abbreviations():
    assert parse_when("2026-11-10 14:00 America/Los_Angeles") == "2026-11-10T22:00:00+00:00"
    assert parse_when("2026-11-10 2:00 PM PT") == "2026-11-10T22:00:00+00:00"
    assert parse_when("Nov 10, 2026 2:00 PM ET") == "2026-11-10T19:00:00+00:00"
    assert parse_when("2026-11-10 14:00 cst") == "2026-11-10T20:00:00+00:00"
    assert parse_when("2026-07-10T14:00 PST") == "2026-07-10T21:00:00+00:00"  # Pacific, in daylight time
    assert parse_when("2026-11-10 14:00 UTC") == "2026-11-10T14:00:00+00:00"
    with pytest.raises(ValueError) as e:
        parse_when("next tuesday-ish")
    assert "-08:00" in str(e.value) and "America/Los_Angeles" in str(e.value)
    with pytest.raises(ValueError, match="time zone 'Mars/Base'"):
        parse_when("2026-11-10 14:00 Mars/Base")


def test_mcp_logs_an_interview_with_a_zone_name(store):
    job, dana = _dana(store)
    out = structured(call(build_server(store), "log_activity", candidate_id=dana, kind="interview",
                          summary="Intro", when="2026-12-10 14:00 America/Los_Angeles", job_id=job["id"]))
    assert out["logged"]["at"] == "2026-12-10T22:00:00+00:00"


# --------------------------------------------------- 4. calendar days
def test_ago_counts_calendar_days(utc_clock):
    now = datetime(2026, 11, 5, 1, 20, tzinfo=timezone.utc)
    assert _ago("2026-11-04T23:00:00+00:00", now) == "yesterday"
    assert _ago("2026-11-05T00:10:00+00:00", now) == "today"
    assert _ago("2026-11-01T12:00:00+00:00", now) == "4 days ago"
    assert actions.local_days(datetime(2026, 11, 4, 23, tzinfo=timezone.utc), now) == 1


def test_ago_and_days_in_stage_use_local_days(chicago):
    now = datetime(2026, 11, 5, 7, 20, tzinfo=timezone.utc)  # 1:20 AM CST
    assert _ago("2026-11-05T04:00:00+00:00", now) == "yesterday"  # 10 PM CST the day before
    assert _ago("2026-11-05T06:30:00+00:00", now) == "today"


# ------------------------------------------------- 5. zone on the screen
def test_today_and_the_interview_form_show_the_zone(store):
    job, dana = _dana(store)
    at = _at(1)
    store.log(dana, "interview", "Screen", at=at, job_id=job["id"])
    c = local_client(store)
    zone = datetime.now().astimezone().tzname()
    assert actions.local_time(at.isoformat(), actions.IV_TIME) in c.get("/").text
    page = c.get(f"/candidates/{dana}").text
    assert f'<span class="fine zone">{zone}</span>' in page


# ---------------------------------------------- 6. plain replies
def test_update_interview_and_list_jobs_reply_in_plain_lines(store):
    job, dana = _dana(store)
    iv = store.log(dana, "interview", "Phone screen", at=_at(2), job_id=job["id"])
    server = build_server(store)
    out = text(call(server, "update_interview", interview_id=iv["id"], when=_at(3).isoformat()))
    first = out.splitlines()[0]
    assert first.startswith("Moved Dana Okafor's interview (Phone screen) to ") and "(was " in first
    res = call(server, "update_interview", interview_id=iv["id"], cancel=True)
    assert text(res).startswith("Cancelled Dana Okafor's interview") and structured(res)["interview"]["status"] == "cancelled"
    res = call(server, "list_jobs")
    lines = text(res).splitlines()
    assert lines[0] == "1 open job:" and lines[1].startswith("- Backend Engineer (Acme) (job id")
    assert "Screening 1" in lines[1]
    assert json.loads(lines[-1])[0]["id"] == job["id"] and structured(res)["jobs"][0]["id"] == job["id"]


# ---------------------------------------------- 7. filled: offer to close
def test_move_to_hired_that_fills_the_last_opening_hints_closing(store):
    job, dana = _dana(store, openings=2)
    bo = store.add_candidate(name="Bo Lin")["id"]
    store.add_to_pipeline(bo, job["id"], "Screening")
    cy = store.add_candidate(name="Cy Park")["id"]
    store.add_to_pipeline(cy, job["id"], "Screening")
    server = build_server(store)
    first = structured(call(server, "move_stage", candidate_id=dana, job_id=job["id"], stage="Hired"))
    assert "close_out_hint" not in first
    res = call(server, "move_stage", candidate_id=bo, job_id=job["id"], stage="Hired")
    hint = structured(res)["close_out_hint"]
    assert "fills the last opening" in hint and "close the job" in hint and "Cy Park" in hint
    assert "fills the last opening" in text(res)


# ---------------------------------------------- 8. interviewer
def test_interviewer_shows_on_today_timeline_and_status(store):
    job, dana = _dana(store)
    server = build_server(store)
    out = structured(call(server, "log_activity", candidate_id=dana, kind="interview", summary="HM screen",
                          when=_at(2).isoformat(), job_id=job["id"], interviewer="Priya Raman"))
    assert out["logged"]["interviewer"] == "Priya Raman"
    iv = out["logged"]["id"]
    row = next(i for i in actions.action_items(store)["items"] if i["kind"] == "interview")
    assert "with Priya Raman" in row["detail"]
    assert "(HM screen, with Priya Raman)" in actions.job_status_text(store, job["id"])
    assert "Interviewer: Priya Raman" in local_client(store).get(f"/candidates/{dana}").text
    res = call(server, "update_interview", interview_id=iv, interviewer="Sam Lee")
    assert "Interviewer for Dana Okafor's interview (HM screen): Sam Lee" in text(res)
    assert store.get_interview(iv)["data"]["interviewer"] == "Sam Lee"
    call(server, "update_interview", interview_id=iv, interviewer="")
    assert "interviewer" not in store.get_interview(iv)["data"]


# ---------------------------------------------- 9. hiring managers
def test_status_digest_by_hiring_manager(store):
    a = store.add_job("Senior Data Engineer", company="Lumen Health", hiring_manager="Priya Raman")
    b = store.add_job("Clinical PM", company="Lumen Health", hiring_manager="Dana Whitfield")
    for name, j in (("Ana", a), ("Ben", b)):
        cid = store.add_candidate(name=name)["id"]
        store.add_to_pipeline(cid, j["id"], "Screening")
    server = build_server(store)
    res = call(server, "status_digest", hiring_manager="priya")
    out = text(res)
    assert out.startswith("Status update for Priya Raman,") and "Senior Data Engineer" in out and "Clinical PM" not in out
    assert structured(res)["hiring_managers"][0]["hiring_manager"] == "Priya Raman"
    c = local_client(store)
    assert "<th>Company</th>" not in c.get("/jobs").text  # one company: no table
    actions.set_inhouse(store, True)
    page = c.get("/jobs").text
    assert "<th>Hiring manager</th>" in page and "Copy status for Priya Raman" in page
    digest = actions.status_digest(store)
    assert "- Dana Whitfield: 1 open job" in digest and "- Priya Raman: 1 open job" in digest


# ---------------------------------------------- 10. Today row actions
def test_today_interview_rows_link_to_reschedule_and_outcome(store):
    job, dana = _dana(store)
    soon = store.log(dana, "interview", "Screen", at=_at(1), job_id=job["id"])
    started = store.log(dana, "interview", "Coffee", at=_at(0, -1), job_id=job["id"])
    c = local_client(store)
    home = c.get("/").text
    assert f'href="/candidates/{dana}?iv={soon["id"]}#iv-{soon["id"]}">Reschedule</a>' in home
    assert f'?iv={started["id"]}#iv-{started["id"]}">Record outcome</a>' in home
    assert f'?iv={soon["id"]}#iv-{soon["id"]}">Record outcome' not in home  # not before it happens
    page = c.get(f"/candidates/{dana}?iv={soon['id']}").text
    assert f'id="iv-{soon["id"]}" open' in page and f'id="iv-{started["id"]}" >' in page
