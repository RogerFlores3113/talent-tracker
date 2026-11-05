"""Cycle 2, fixer D: interviews as records, one status update, job headcount, stage outcomes, to-dos, Today."""
import shutil
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from talent import actions
from talent import store as store_mod
from talent.mcp_server import build_server
from talent.mcp_views import view_html
from talent.sample_data import load_sample_data, sample_ids
from talent.store import Store

from conftest import flash_of, local_client
from test_mcp import call, structured, text


def _at(days=2, hours=0):
    return (datetime.now(timezone.utc) + timedelta(days=days, hours=hours)).replace(second=0, microsecond=0)


def _msg(r, key="notice"):
    return flash_of(r, key)


def _setup(store, stages=None):
    job = store.add_job("Backend", company="Acme", stages=stages)
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    return job, ana


# ------------------------------------------------------------ 1. interviews
def test_interview_reschedule_cancel_and_outcome_in_the_store(store):
    job, ana = _setup(store)
    store.add_to_pipeline(ana, job["id"], "Interviewing")
    iv = store.log(ana, "interview", "Onsite", at=_at(3), job_id=job["id"])
    moved = store.update_interview(iv["id"], at=_at(5))
    assert moved["at"] == _at(5).isoformat() and moved["data"]["rescheduled_from"] == [iv["at"]]
    assert actions.next_interviews(store, job["id"]) == {ana: _at(5).isoformat()}
    assert [t["id"] for t in store.upcoming_interviews(days=14)] == [iv["id"]]  # one interview, not two

    store.update_interview(iv["id"], cancel=True)
    assert store.upcoming_interviews(days=14) == [] and actions.next_interviews(store, job["id"]) == {}
    assert [t for t in store.timeline(ana) if t["id"] == iv["id"]][0]["status"] == "cancelled"  # kept, marked
    store.update_interview(iv["id"], restore=True)
    assert actions.next_interviews(store, job["id"]) == {ana: _at(5).isoformat()}

    past = store.log(ana, "interview", "Screen", at=_at(-1), job_id=job["id"])
    done = store.update_interview(past["id"], outcome="no show", note="Didn't join")
    assert done["status"] == "no_show" and done["data"]["outcome_note"] == "Didn't join"
    with pytest.raises(ValueError, match="passed, not passed, no show"):
        store.update_interview(past["id"], outcome="great")
    with pytest.raises(ValueError, match="Say what to change"):
        store.update_interview(past["id"])
    with pytest.raises(store_mod.NotFound):
        store.update_interview(past["id"], cancel=True, candidate_id=ana + 99)


def test_interview_controls_on_the_timeline(store):
    job, ana = _setup(store)
    store.add_to_pipeline(ana, job["id"], "Interviewing")
    iv = store.log(ana, "interview", "Onsite", at=_at(3), job_id=job["id"])
    c = local_client(store)
    page = c.get(f"/candidates/{ana}").text
    assert f'action="/candidates/{ana}/interviews/{iv["id"]}"' in page and "Reschedule" in page
    assert "Cancel interview" in page and "Save outcome" in page

    new = _at(6).astimezone().strftime("%Y-%m-%dT%H:%M")
    r = c.post(f"/candidates/{ana}/interviews/{iv['id']}", data={"op": "reschedule", "at": new},
               follow_redirects=False)
    assert r.status_code == 303 and "Moved the interview" in _msg(r)
    assert actions.next_interviews(store, job["id"])[ana] == _at(6).isoformat()
    assert "moved from" in c.get(f"/candidates/{ana}").text

    r = c.post(f"/candidates/{ana}/interviews/{iv['id']}", data={"op": "cancel"}, follow_redirects=False)
    assert "stays on the timeline" in _msg(r)
    page = c.get(f"/candidates/{ana}").text
    assert "Cancelled" in page and "Put back on the schedule" in page
    assert not [i for i in actions.action_items(store)["items"] if i["kind"] == "interview"]

    # An interview still in the future can't have an outcome yet (tests/test_c2_f.py); move it to the past.
    past = _at(-1).astimezone().strftime("%Y-%m-%dT%H:%M")
    c.post(f"/candidates/{ana}/interviews/{iv['id']}", data={"op": "reschedule", "at": past})
    r = c.post(f"/candidates/{ana}/interviews/{iv['id']}", data={"op": "outcome", "outcome": "passed",
                                                                 "note": "Strong system design"}, follow_redirects=False)
    assert _msg(r) == "Saved: Passed."
    page = c.get(f"/candidates/{ana}").text
    assert "How it went: Strong system design" in page
    r = c.post(f"/candidates/{ana}/interviews/{iv['id']}", data={"op": "outcome", "outcome": "meh"},
               follow_redirects=False)
    assert "how the interview went" in _msg(r, "error")
    assert c.post(f"/candidates/{ana}/interviews/99999", data={"op": "cancel"}).status_code == 404


def test_mcp_update_interview_and_log_activity_warns_about_a_second_one(store):
    job, ana = _setup(store)
    store.add_to_pipeline(ana, job["id"], "Interviewing")
    server = build_server(store)
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    assert tools["update_interview"].annotations.destructiveHint is True  # an edit, not a delete
    first = structured(call(server, "log_activity", candidate_id=ana, kind="interview", summary="Onsite",
                            when=_at(3).isoformat(), job_id=job["id"]))["logged"]
    second = structured(call(server, "log_activity", candidate_id=ana, kind="interview", summary="Onsite again",
                             when=_at(4).isoformat(), job_id=job["id"]))
    assert f"id {first['id']}" in second["warning"] and "update_interview" in second["warning"]
    out = structured(call(server, "update_interview", interview_id=first["id"], cancel=True))
    assert out["interview"]["status"] == "cancelled"
    out = structured(call(server, "update_interview", interview_id=second["logged"]["id"], when=_at(8).isoformat()))
    assert out["interview"]["status"] == "scheduled" and out["interview"]["rescheduled_from"]
    assert [t["id"] for t in store.upcoming_interviews(days=14)] == [second["logged"]["id"]]


def test_interviews_only_for_open_applications(store):
    job, ana = _setup(store)
    lee = store.add_candidate(name="Lee Park")["id"]
    store.add_to_pipeline(ana, job["id"], "Hired")
    store.add_to_pipeline(lee, job["id"], "Interviewing")
    for cid in (ana, lee):
        store.log(cid, "interview", "Round 2", at=_at(2), job_id=job["id"])
    assert set(actions.next_interviews(store, job["id"])) == {lee}
    board = structured(call(build_server(store), "job_pipeline", job_id=job["id"]))
    hired = next(s for s in board["stages"] if s["name"] == "Hired")["candidates"][0]
    assert "next_interview" not in hired


# ---------------------------------------------------------- 2. status text
def test_one_status_update_for_the_app_and_the_ai(store):
    job, ana = _setup(store)
    lee, bo = (store.add_candidate(name=n)["id"] for n in ("Lee Park", "Bo Li"))
    store.add_to_pipeline(ana, job["id"], "Screening")
    store.add_to_pipeline(lee, job["id"], "Rejected")
    store.add_to_pipeline(bo, job["id"], "Sourced")
    text_ = actions.job_status_text(store, job["id"])
    lines = text_.splitlines()
    assert lines[0] == "Backend, Acme"
    assert "1 candidate in process, none hired yet. 1 out." in lines and "Bo Li" not in text_  # Sourced is internal
    assert "Changes since last week:" in lines
    assert "- Out: Lee Park (Rejected)" in lines and any(l.startswith("- New: ") and "Ana Ruiz in Screening" in l
                                                          for l in lines)
    res = call(build_server(store), "job_pipeline", job_id=job["id"])
    assert res.content[0].text == text_ and "Candidate-provided" in res.content[1].text
    assert structured(res)["counts"] == {"active": 2, "hired": 0, "out": 1}

    left = actions.job_status_text(store, job["id"], omit_stages=["sourced", "rej"])
    assert "Bo Li" not in left and "Lee Park" not in left and "Ana Ruiz" in left
    assert "1 candidate in process, none hired yet." in left and "out." not in left  # counts cover who's shown
    mcp_left = call(build_server(store), "job_pipeline", job_id=job["id"], omit_stages=["Sourced", "Rejected"])
    assert mcp_left.content[0].text == left
    page = local_client(store).get(f"/jobs/{job['id']}?omit=Sourced&omit=Rejected").text
    assert 'name="omit" value="Sourced" checked' in page and "Bo Li (today)" not in page


def test_old_moves_are_not_changes_and_placed_wording(store):
    job = store.add_job("Exec search", company="Northwind",
                        stages=["Sourced", "Submitted", "Placed", {"name": "Client passed", "outcome": "lost"}])
    ana = store.add_candidate(name="Ana")["id"]
    store.add_to_pipeline(ana, job["id"], "Submitted")
    with store._lock, store._conn:
        store._conn.execute("UPDATE timeline SET at = '2020-01-01T00:00:00+00:00'")
    text_ = actions.job_status_text(store, job["id"])
    assert "Changes since last week: none." in text_ and "none placed yet" in text_


def test_status_digest_on_jobs_page_and_in_mcp(store):
    a = store.add_job("Backend", company="Acme")
    store.add_job("Designer", company="Acme")
    r = store.add_job("SRE", company="Ramp", openings=2)
    ana = store.add_candidate(name="Ana")["id"]
    store.add_to_pipeline(ana, a["id"], "Screening")
    store.add_to_pipeline(store.add_candidate(name="Bo")["id"], r["id"], "Hired")
    digest = actions.status_digest(store)
    assert "- Acme: 2 open jobs, 1 in process, 0 of 2 openings filled." in digest
    assert "- Ramp: 1 open job, 0 in process, 1 of 2 openings filled." in digest
    assert "Backend, Acme" in digest and "SRE, Ramp" in digest and "1 hired, 2 openings" in digest
    only = call(build_server(store), "status_digest", company="ramp")
    assert "SRE, Ramp" in only.content[0].text and "Backend" not in only.content[0].text
    assert structured(only)["companies"][0]["company"] == "Ramp"
    assert "No open jobs for Zed." in text(call(build_server(store), "status_digest", company="Zed"))
    page = local_client(store).get("/jobs").text
    assert "Copy status for all open jobs" in page and "Copy status for Acme" in page


# ------------------------------------------------------- 3. job headcount
def test_job_fields_header_and_filled(store):
    c = local_client(store)
    r = c.post("/jobs", data={"title": "Designer", "company": "Acme", "hiring_manager": "Dana Lee",
                              "hiring_manager_email": "dana@acme.com", "openings": "2", "target_date": "2026-12-15",
                              "min_active": "3"}, follow_redirects=False)
    jid = int(r.headers["location"].split("/")[2])
    job = store.get_job(jid)
    assert (job["hiring_manager"], job["openings"], job["target_date"], job["min_active"]) == ("Dana Lee", 2, "2026-12-15", 3)
    a, b = (store.add_candidate(name=n)["id"] for n in ("Ana", "Bo"))
    store.add_to_pipeline(a, jid, "Hired")
    page = c.get(f"/jobs/{jid}").text
    assert "1 hired, 2 openings" in page and "Hiring manager:" in page and "Dana Lee" in page
    assert "This role is filled" not in page
    assert "Hiring manager: Dana Lee" in actions.job_status_text(store, jid)
    store.add_to_pipeline(b, jid, "Hired")
    page = c.get(f"/jobs/{jid}").text
    assert "This role is filled. Close it?" in page
    r = c.post(f"/jobs/{jid}", data={"status": "closed"}, follow_redirects=False)
    assert store.get_job(jid)["status"] == "closed" and "Job closed" in _msg(r)
    edit = c.get(f"/jobs/{jid}/edit").text
    assert 'name="openings"' in edit and 'value="dana@acme.com"' in edit
    r = c.post(f"/jobs/{jid}", data={"title": "Designer", "openings": "zero"})
    assert "Openings must be a whole number" in r.text


def test_mcp_create_and_update_job_fields(store):
    server = build_server(store)
    job = structured(call(server, "create_job", title="SRE", company="Ramp", hiring_manager="Sam",
                          openings=2, target_date="Dec 1, 2026", min_active=2))
    assert job["openings"] == 2 and job["target_date"] == "2026-12-01" and job["min_active"] == 2
    upd = structured(call(server, "update_job", job_id=job["id"], hiring_manager_email="sam@ramp.com", openings=3))
    assert upd["hiring_manager_email"] == "sam@ramp.com" and upd["openings"] == 3
    with pytest.raises(Exception, match="email address"):
        call(server, "update_job", job_id=job["id"], hiring_manager_email="nope")


def test_thin_check_uses_job_target_and_skips_offers_and_filled(store):
    a = store.add_job("Staff Backend Engineer", company="Ramp")
    store.add_job("Exec", company="Acme")
    filled = store.add_job("One seat", company="Initech", min_active=1)
    small = store.add_job("Founding Eng", company="Mine", min_active=1)
    for n, j, st in (("Ana", a, "Screening"), ("Bo", a, "Sourced")):
        store.add_to_pipeline(store.add_candidate(name=n)["id"], j["id"], st)
    exec_job = [j for j in store.list_jobs() if j["title"] == "Exec"][0]
    store.add_to_pipeline(store.add_candidate(name="Cy")["id"], exec_job["id"], "Offer")
    store.add_to_pipeline(store.add_candidate(name="Di")["id"], filled["id"], "Hired")
    store.add_to_pipeline(store.add_candidate(name="Ed")["id"], small["id"], "Sourced")
    thin = next(i for i in actions.action_items(store)["items"] if i["kind"] == "thin")
    assert thin["title"] == "Staff Backend Engineer (Ramp) has 2 active candidates. Aim for at least 3."


def test_migration_v4_from_v3(tmp_path, monkeypatch):
    path = tmp_path / "v3.db"
    shutil.copy(store_mod.Path(__file__).parent / "fixtures" / "v1.db", path)
    steps = store_mod._MIGRATIONS
    monkeypatch.setattr(store_mod, "_MIGRATIONS", steps[:3])
    s = Store(path)
    ts = "2026-11-01T00:00:00+00:00"
    with s._lock, s._conn:  # written the way the v3 code wrote them
        jid = s._conn.execute("INSERT INTO jobs(title, status, created_at, updated_at) VALUES ('Founding Eng', 'open', ?, ?)",
                              (ts, ts)).lastrowid
        s._conn.executemany("INSERT INTO job_stages(job_id, name, position, outcome) VALUES (?, ?, ?, ?)",
                            [(jid, "Sourced", 0, "open"), (jid, "Not moving forward", 1, "open"), (jid, "Intro", 2, "open"),
                             (jid, "Hired", 3, "hired"), (jid, "Passed", 4, "open")])
        sid = s._conn.execute("INSERT INTO suggestions(text, created_at) VALUES ('Call Ana', ?)", (ts,)).lastrowid
        s._conn.execute("INSERT INTO settings(key, value) VALUES (?, '2026-12-01')", (f"followup:{sid}",))
    job = {"id": jid}
    s.close()
    monkeypatch.setattr(store_mod, "_MIGRATIONS", steps)
    s = Store(path)
    assert s.schema_version >= 4
    assert s.get_job(job["id"])["openings"] == 1
    outcomes = {st["name"]: st["outcome"] for st in s.job_stages(job["id"])}
    assert outcomes["Passed"] == "lost" and outcomes["Not moving forward"] == "open"  # only tail stages change
    assert s._row("SELECT due_on FROM suggestions WHERE id = ?", (sid,))["due_on"] == "2026-12-01"
    assert not [k for k in s.settings() if k.startswith("followup:")]
    s.close()


# ----------------------------------------------------------------- 4. stages
def test_exit_names_count_as_out_and_explicit_outcomes(store):
    for name in ("Passed", "No hire", "Not moving forward", "Rejected by client", "Declined offer", "not selected"):
        assert store_mod.stage_outcome(name) == "lost", name
    server = build_server(store)
    job = structured(call(server, "create_job", title="Founding Engineer",
                          stages=["Sourced", "Intro call", "Offer", "Hired", "Passed",
                                  {"name": "Parked", "outcome": "out"}]))
    assert job["stage_outcomes"]["Passed"] == "lost" and job["stage_outcomes"]["Parked"] == "lost"
    jid = job["id"]
    out = structured(call(server, "edit_job_stages", job_id=jid,
                          add=[{"name": "Ghosted", "outcome": "lost"}],
                          rename={"Passed": {"name": "No go", "outcome": "lost"}}))
    assert out["stage_outcomes"] == {"Sourced": "open", "Intro call": "open", "Offer": "open", "Hired": "hired",
                                     "No go": "lost", "Parked": "lost", "Ghosted": "lost"}
    with pytest.raises(Exception, match="change what Parked counts as"):  # existing stages: the recruiter's call
        call(server, "edit_job_stages", job_id=jid, outcomes={"Parked": "open"})
    with pytest.raises(Exception, match="Say what Not moving forward should count as"):
        call(server, "edit_job_stages", job_id=jid, rename={"Intro call": "Not moving forward"})
    store.rename_stage(jid, "Intro call", "Not moving forward", outcome="lost")
    assert {s["name"]: s["outcome"] for s in store.job_stages(jid)}["Not moving forward"] == "lost"
    with pytest.raises(ValueError, match="outcome must be open"):
        store.add_stage(jid, "Odd", outcome="maybe")


def test_passed_people_are_not_in_the_running(store):
    job = store.add_job("Founding Engineer", stages=["Sourced", "Onsite", "Hired", "Passed"])
    for n, st in (("Ana", "Onsite"), ("Bo", "Passed")):
        store.add_to_pipeline(store.add_candidate(name=n)["id"], job["id"], st)
    assert store.list_jobs()[0]["active_count"] == 1
    assert "1 candidate in process, none hired yet. 1 out." in actions.job_status_text(store, job["id"])


# ------------------------------------------------------------ 5. follow-ups
def test_parse_day_reads_weekday_words():
    thu = date(2026, 11, 5)  # a Thursday
    assert actions.parse_day("friday", today=thu) == date(2026, 11, 6)
    assert actions.parse_day("next tue", today=thu) == date(2026, 11, 10)
    assert actions.parse_day("Thursday", today=thu) == date(2026, 11, 12)  # the coming one, not today
    assert actions.parse_day("tomorrow", today=thu) == date(2026, 11, 6)
    assert actions.parse_day("in 2 weeks", today=thu) == date(2026, 11, 19)
    with pytest.raises(ValueError, match="friday or tomorrow"):
        actions.parse_day("someday", today=thu)


def test_follow_up_rows_show_note_job_and_due_day(store):
    job, ana = _setup(store)
    c = local_client(store)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    c.post(f"/candidates/{ana}/log", data={"kind": "call", "body": "check offer signed", "job_id": str(job["id"]),
                                           "follow_up": yesterday})
    row = next(i for i in actions.action_items(store)["items"] if i["kind"] == "suggestion")
    assert row["title"] == "Follow up with Ana Ruiz" and row["overdue"]
    assert row["detail"] == "check offer signed · Backend, Acme" and row["due_text"].startswith("Overdue, due ")
    home = c.get("/").text
    assert "check offer signed" in home and "badge overdue" in home
    mcp_text = text(call(build_server(store), "list_action_items"))
    assert "check offer signed" in mcp_text and "[Overdue, due " in mcp_text
    s = structured(call(build_server(store), "add_suggestion", text="Send Ana the offer", due="friday"))
    assert s["due_on"] == actions.parse_day("friday").isoformat()


def test_bad_follow_up_date_keeps_the_note(store):
    _, ana = _setup(store)
    r = local_client(store).post(f"/candidates/{ana}/log", data={"kind": "call", "body": "Left a voicemail",
                                                                 "follow_up": "whenever"}, follow_redirects=False)
    err = _msg(r, "error")
    assert err.startswith("Saved, but the follow-up wasn't added.") and "whenever" in err
    assert store.timeline(ana)[0]["body"] == "Left a voicemail"


def test_add_todo_on_today_and_the_job_page(store):
    job, _ = _setup(store)
    c = local_client(store)
    home = c.get("/").text
    assert 'action="/actions/todo"' in home and "Add to-do" in home
    r = c.post("/actions/todo", data={"text": "Send Acme the weekly update", "job_id": str(job["id"])},
               follow_redirects=False)
    assert _msg(r) == "To-do added."
    later = (date.today() + timedelta(days=3)).isoformat()
    r = c.post("/actions/todo", data={"text": "Invoice Acme", "job_id": str(job["id"]), "due": later},
               follow_redirects=False)
    assert "shows on Today from" in _msg(r)
    page = c.get(f"/jobs/{job['id']}").text
    assert "Send Acme the weekly update" in page and "Invoice Acme" in page and "To-dos for this job" in page
    rows = [i for i in actions.action_items(store)["items"] if i["kind"] == "suggestion"]
    assert [i["title"] for i in rows] == ["Send Acme the weekly update"] and rows[0]["href"] == f"/jobs/{job['id']}"
    r = c.post("/actions/todo", data={"text": "  "}, follow_redirects=False)
    assert _msg(r, "flash_error") == "Write what the to-do is."


# ------------------------------------------------------------------ 6. Today
def test_today_next_week_on_hold_twins_and_sample(store):
    a = store.add_job("Backend", company="Acme", location="Austin")
    b = store.add_job("Backend", company="Acme", location="Remote")
    hold = store.add_job("Paused role", company="Acme")
    ana, bo, cy = (store.add_candidate(name=n)["id"] for n in ("Ana", "Bo", "Cy"))
    store.add_to_pipeline(ana, a["id"], "Interviewing")
    store.add_to_pipeline(bo, b["id"], "Interviewing")
    store.add_to_pipeline(cy, hold["id"], "Interviewing")
    store.log(ana, "interview", "Onsite", at=_at(2), job_id=a["id"])
    store.log(bo, "interview", "Final", at=_at(10), job_id=b["id"])
    store.log(cy, "interview", "Screen", at=_at(1), job_id=hold["id"])
    store.update_job(hold["id"], status="on hold")
    res = actions.action_items(store)
    now_rows = [i for i in res["items"] if i["kind"] == "interview"]
    assert [i["title"] for i in now_rows] == ["Interview with Cy", "Interview with Ana"]
    assert "job on hold" in now_rows[0]["detail"] and "Backend, Acme (Austin)" in now_rows[1]["detail"]
    assert [i["title"] for i in res["next_week"]] == ["Interview with Bo"]
    home = local_client(store).get("/").text
    assert "Next week" in home and "job on hold" in home
    assert "Next week:" in text(call(build_server(store), "list_action_items"))


def test_sample_badge_on_today_rows(store):
    load_sample_data(store)
    people, _ = sample_ids(store)
    home = local_client(store).get("/").text
    today = home.split('class="today"')[1].split("</section>")[0]
    assert "badge sample" in today and people


# ------------------------------------------------------------- 7. chat view
def test_in_chat_board_dims_closed_columns_and_counts():
    html = view_html("pipeline")
    assert '" closed"' in html and "in the running" in html and ".col.closed{opacity" in html


# ---------------------------------------------------------- 8. out reasons
def test_out_stage_reason_goes_on_the_timeline(store):
    job, ana = _setup(store)
    store.add_to_pipeline(ana, job["id"], "Screening")
    c = local_client(store)
    page = c.get(f"/jobs/{job['id']}").text
    assert "data-out=1" in page and "Pick a reason" in page
    c.post(f"/jobs/{job['id']}/move", data={"candidate_id": str(ana), "stage": "Rejected",
                                             "notes": "Reason: Not a fit"}, headers={"HX-Request": "true"})
    assert store.candidate_pipelines(ana)[0]["stage"] == "Rejected"
    assert any(t["kind"] == "note" and t["body"] == "Reason: Not a fit" for t in store.timeline(ana))
