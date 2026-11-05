"""Cycle 6, fixer B: client updates stay per client and per hiring manager (no job-less interview leaks, agency
updates split per manager, a held hire holds its close), clients before their first job, the send-out bugs (a
repeat Mark as sent, day names, over-filled counts, a slim update_job), and submittals with the client feedback
chase."""
import json
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from talent import actions, chase, reports
from talent.mcp_server import build_server

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def data(res) -> dict:
    return res.structuredContent if isinstance(res, CallToolResult) else res[1].get("result", res[1])


def _at(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _agency(store, company: str, title: str, manager: str | None = None, email: str | None = None):
    return store.add_job(title, company=company, stages=actions.stage_set("agency"),
                         **({"hiring_manager": manager} if manager else {}),
                         **({"hiring_manager_email": email} if email else {}))


def _age(store, cid: int, jid: int, days: int) -> None:
    with store._lock, store._conn:
        store._conn.execute("UPDATE applications SET stage_changed_at = ? WHERE candidate_id = ? AND job_id = ?",
                            (_at(-days), cid, jid))


def _two_clients(store):
    hfl = _agency(store, "Harbor Freight Labs", "Staff Robotics Engineer")
    acme = _agency(store, "Acme Payments", "Senior Backend Engineer")
    lena = store.add_candidate(name="Lena Hoffmann")["id"]
    store.add_to_pipeline(lena, hfl["id"], "Submitted")
    store.add_to_pipeline(lena, acme["id"])
    reports.save_client(store, "acme payments", contacts="Jordan <jordan@acmepayments.com>")
    return hfl, acme, lena


# ------------------------------------------------- 1. job-less interviews never leak
def test_a_jobless_interview_for_someone_on_two_clients_goes_in_neither_update(store):
    hfl, acme, lena = _two_clients(store)
    iv = store.log(lena, "interview", "Client interview with Dana Reyes (HFL)", at=_at(3))
    for key in ("acme payments", "harbor freight labs"):
        rep = reports.report(store, key)
        out = reports.report_text(rep)
        assert "Dana Reyes" not in out and "Upcoming interviews" not in out
        assert rep["loose_interviews"][0]["interview_id"] == iv["id"]
        assert any("Pick the job" in n for n in reports.recruiter_notes(rep))
    link = reports.email_link(reports.report(store, "acme payments"), out)
    assert link is None or "Dana" not in link["href"]
    assert all(r["interviews"] == 0 for r in reports.overview(store))
    c = local_client(store)
    page = c.get("/clients/acme-payments").text
    assert f'href="/interviews/{iv["id"]}/job"' in page
    # Picking the job puts it in that client's update only.
    pick = c.get(f"/interviews/{iv['id']}/job").text
    assert "Staff Robotics Engineer" in pick and "Senior Backend Engineer" in pick
    r = c.post(f"/interviews/{iv['id']}/job", data={"job_id": hfl["id"]}, follow_redirects=False)
    assert "now for Staff Robotics Engineer" in flash_of(r)
    assert "Upcoming interviews" in reports.report_text(reports.report(store, "harbor freight labs"))
    assert "Dana Reyes" not in reports.report_text(reports.report(store, "acme payments"))
    r = c.post(f"/interviews/{iv['id']}/job", data={"job_id": "999"}, follow_redirects=False)
    assert "Pick one of the jobs" in flash_of(r, "flash_error")


def test_a_jobless_interview_on_one_open_job_still_counts(store):
    job = _agency(store, "Acme", "Backend")
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    store.add_to_pipeline(ana, job["id"], "Submitted")
    store.log(ana, "interview", "Client interview", at=_at(2))
    assert "Upcoming interviews:" in reports.report_text(reports.report(store, "acme"))


def test_interviews_page_shows_no_job_with_a_pick_link(store):
    _hfl, _acme, lena = _two_clients(store)
    iv = store.log(lena, "interview", "Client interview", at=_at(0.2))
    page = local_client(store).get("/interviews").text
    assert "No job" in page and f"/interviews/{iv['id']}/job" in page


def test_log_activity_asks_for_the_job_when_on_two(store):
    _hfl, _acme, lena = _two_clients(store)
    res = call(build_server(store), "log_activity", candidate_id=lena, kind="interview", summary="Client interview",
               when=_at(3))
    assert "is on 2 open jobs" in text(res) and "job_id" in text(res)


# ------------------------------------------------- 2. a client before its first job
def test_new_client_form_and_new_job_prefill(store):
    c = local_client(store)
    assert 'action="/clients/new"' in c.get("/clients").text
    r = c.post("/clients/new", data={"name": "Pinecrest Capital", "update_day": "Friday", "time_zone": "ET",
                                     "contacts": "Sam <sam@pinecrest.com>"}, follow_redirects=False)
    assert r.headers["location"].startswith("/clients/pinecrest-capital") and "Added Pinecrest Capital" in flash_of(r)
    saved = reports.client(store, "pinecrest capital")
    assert saved["update_day"] == "Friday" and saved["time_zone"] == "America/New_York" and saved["jobs"] == []
    assert "Pinecrest Capital" in c.get("/clients").text
    page = c.get("/clients/pinecrest-capital").text
    assert "/jobs/new?company=Pinecrest+Capital" in page
    assert 'value="Pinecrest Capital"' in c.get("/jobs/new?company=Pinecrest+Capital").text
    r = c.post("/clients/new", data={"name": "pinecrest capital, inc."}, follow_redirects=False)
    assert "already here" in flash_of(r)
    r = c.post("/clients/new", data={"name": "Bad Zone Co", "time_zone": "Mars/Base"}, follow_redirects=False)
    assert "time zone" in flash_of(r, "flash_error") and reports.get_row(store, "bad zone co") is None
    # A job naming it joins the saved client.
    store.add_job("Analyst", company="Pinecrest Capital")
    assert len(reports.client(store, "pinecrest capital")["jobs"]) == 1


def test_ai_create_client_is_logged_and_undoable(store):
    server = build_server(store)
    with pytest.raises(ToolError, match="create_client"):
        call(server, "update_client", client="Harbor Freight Labs", update_day="fri")
    res = call(server, "create_client", name="Harbor Freight Labs", update_day="fri", time_zone="PT")
    assert text(res).startswith('Added the client "Harbor Freight Labs".') and "\n{" not in text(res)
    c = reports.client(store, "harbor freight labs")
    assert c["update_day"] == "Friday" and c["time_zone"] == "America/Los_Angeles"
    again = call(server, "create_client", name="harbor freight labs")
    assert data(again)["created"] is False and "already a client" in text(again)
    made = [a for a in store.ai_actions() if a["kind"] == "client_create"]
    assert len(made) == 1 and made[0]["summary"] == 'Added the client "Harbor Freight Labs"'
    store.undo_ai_action(made[0]["id"])
    assert reports.get_row(store, "harbor freight labs") is None
    # Once a job names it, the add can't be undone.
    call(server, "create_client", name="Globex")
    store.add_job("Backend", company="Globex")
    gid = [a for a in store.ai_actions() if a["kind"] == "client_create" and not a["undone_at"]][0]["id"]
    with pytest.raises(ValueError, match="A job names that client"):
        store.undo_ai_action(gid)


# ------------------------------------------------- 3. one update per hiring manager
def test_agency_updates_split_per_hiring_manager(store):
    be = _agency(store, "Acme", "Senior Backend Engineer", manager="Dev Patel")
    ml = _agency(store, "Acme", "ML Engineer", manager="Kim Lee", email="kim@acme.com")
    a = store.add_candidate(name="Ana Ruiz")["id"]
    b = store.add_candidate(name="Bo Li")["id"]
    store.add_to_pipeline(a, be["id"], "Submitted")
    store.add_to_pipeline(b, ml["id"], "Submitted")
    reports.save_client(store, "acme", contacts="Talent <talent@acme.com>\nDev Patel <dev@acme.com>",
                        update_day=reports.UPDATE_DAYS[datetime.now().astimezone().weekday()])
    parts = reports.reports_for(store, "acme")
    assert [p["manager"] for p in parts] == ["Dev Patel", "Kim Lee"]
    dev, kim = parts
    assert "Ana Ruiz" in reports.report_text(dev) and "Bo Li" not in reports.report_text(dev)
    assert reports.recipients(dev) == ("dev@acme.com", []) and reports.recipients(kim) == ("kim@acme.com", [])
    assert reports.report_text(dev).startswith("Acme: hiring update for Dev Patel, ")
    c = local_client(store)
    page = c.get("/clients/acme").text
    assert "name 2 hiring managers" in page and "Mark this one as sent" in page
    due = c.get("/clients/due").text
    assert "name 2 hiring managers" in due and due.count("Mark as sent</button>") == 2
    # A manager with no email of their own gets no email button, never the general contact.
    store.update_job(ml["id"], hiring_manager_email="")
    kim = reports.reports_for(store, "acme")[1]
    assert reports.recipients(kim) == (None, [])
    # One named manager in agency wording: still one update.
    store.update_job(ml["id"], hiring_manager="")
    assert len(reports.reports_for(store, "acme")) == 1


def test_a_held_hire_holds_back_req_closed(store):
    actions.set_inhouse(store, True)
    job = store.add_job("Senior Backend Engineer", company="Lumen Health", hiring_manager="Dana Whitfield",
                        stages=actions.stage_set("inhouse"))
    tom = store.add_candidate(name="Tomas Okafor")["id"]
    store.add_to_pipeline(tom, job["id"], "Offer")
    server = build_server(store)
    call(server, "move_stage", candidate_id=tom, job_id=job["id"], stage="Hired")
    store.update_job(job["id"], status="closed")
    rep = reports.report(store, "lumen health", manager="dana whitfield")
    out = reports.report_text(rep)
    assert "Req closed" not in out and "No open jobs right now" not in out
    assert "1 open job: Senior Backend Engineer" in out
    assert any("isn't reviewed yet" in n for n in reports.recruiter_notes(rep))
    reports.mark_ai_reviewed(store, [m["action_id"] for m in rep["ai_moves"]])
    out = reports.report_text(reports.report(store, "lumen health", manager="dana whitfield"))
    assert "Hired: Tomas Okafor. Req closed: Senior Backend Engineer." in out


# ------------------------------------------------- 4. send-out bugs
def test_a_repeat_mark_as_sent_with_nothing_new_does_nothing(store):
    job = _agency(store, "Harbor Freight Labs", "Robotics")
    cid = store.add_candidate(name="Lena Hoffmann")["id"]
    store.add_to_pipeline(cid, job["id"], "Submitted")
    server = build_server(store)
    first = data(call(server, "mark_update_sent", client="harbor"))
    res = call(server, "mark_update_sent", client="harbor")
    assert "Already marked" in text(res) and data(res)["already_sent"] is True
    assert len(reports.updates(store, "harbor freight labs")) == 1
    assert reports.updates(store, "harbor freight labs")[0]["id"] == first["update_id"]
    assert len([a for a in store.ai_actions() if a["kind"] == "client_sent"]) == 1
    c = local_client(store)
    r = c.post("/clients/harbor-freight-labs/sent", data={}, follow_redirects=False)
    assert "Already marked as sent" in flash_of(r)
    store.move_stage(cid, job["id"], "Client interview")  # something new: a real send
    c.post("/clients/harbor-freight-labs/sent", data={})
    assert len(reports.updates(store, "harbor freight labs")) == 2


@pytest.mark.parametrize("day", ["fri", "Fri.", "FRIDAY", "fridays"])
def test_day_names_are_accepted(store, day):
    _agency(store, "Acme", "Backend")
    call(build_server(store), "update_client", client="acme", update_day=day)
    assert reports.client(store, "acme")["update_day"] == "Friday"
    assert reports.save_client(store, "acme", update_day="thurs")["update_day"] == "Thursday"
    with pytest.raises(ValueError, match="day of the week"):
        reports.save_client(store, "acme", update_day="t")


def test_list_jobs_and_jobs_page_show_overage(store):
    job = store.add_job("Backend", company="Acme", openings=2)
    other = store.add_job("Data", company="Globex")
    for i in range(10):
        cid = store.add_candidate(name=f"Person {i}")["id"]
        store.add_to_pipeline(cid, job["id"], "Hired")
    cid = store.add_candidate(name="Solo")["id"]
    store.add_to_pipeline(cid, other["id"], "Screening")
    out = text(call(build_server(store), "list_jobs"))
    assert "10 hired for 2 openings (over by 8)" in out and "of 2 hired" not in out
    assert "over by 8" in local_client(store).get("/jobs").text


def test_update_job_reply_is_slim_and_hints_only_on_close(store):
    job = store.add_job("Backend", company="Acme")
    for i in range(30):
        cid = store.add_candidate(name=f"Person {i}", about="x" * 200)["id"]
        store.add_to_pipeline(cid, job["id"], "Screening")
    server = build_server(store)
    res = call(server, "update_job", job_id=job["id"], hiring_manager="Dana")
    assert len(text(res)) < 300 and "close_out_hint" not in data(res) and "still_in_play" not in data(res)
    assert len(json.dumps(data(res))) < 2000
    res = call(server, "update_job", job_id=job["id"], status="closed")
    d = data(res)
    assert len(d["still_in_play"]) == 10 and d["still_in_play_count"] == 30 and "Don't move anyone" in d["close_out_hint"]
    assert len(json.dumps(d)) < 5000


# ------------------------------------------------- 5. submittals and the chase
def _robotics(store):
    job = _agency(store, "Harbor Freight Labs", "Staff Robotics Engineer", manager="Dana Reyes",
                  email="dana@hfl.com")
    cid = store.add_candidate(name="Lena Hoffmann", current_title="Robotics Engineer", current_company="Fetch",
                              linkedin_url="https://www.linkedin.com/in/lena-h")["id"]
    store.add_to_pipeline(cid, job["id"], "Screened")
    store.log(cid, "note", "Strong ROS and C++. Expecting $210k base. Available in 4 weeks.", job_id=job["id"])
    return job, cid


def test_submit_to_client_from_the_board(store):
    job, cid = _robotics(store)
    c = local_client(store)
    board = c.get(f"/jobs/{job['id']}?view=board").text
    assert f"/jobs/{job['id']}/submit/{cid}" in board and "Submit to client" in board
    page = c.get(f"/jobs/{job['id']}/submit/{cid}").text
    assert "Move to Submitted" in page and "Strong ROS" in page and "Comp: Expecting $210k base" in page
    assert "Availability: Available in 4 weeks" in page and "mailto:dana@hfl.com" in page
    r = c.post(f"/jobs/{job['id']}/submit/{cid}", follow_redirects=False)
    assert "Moved to Submitted" in flash_of(r)
    assert store.candidate_pipelines(cid)[0]["stage"] == "Submitted"
    assert reports.submittals(store, "harbor freight labs")[0]["name"] == "Lena Hoffmann"
    page = c.get(f"/jobs/{job['id']}/submit/{cid}").text
    assert "is in Submitted since" in page and "Move to Submitted" not in page
    r = c.post(f"/jobs/{job['id']}/submit/{cid}", follow_redirects=False)
    assert "nothing moved" in flash_of(r)
    assert "—" not in page


def test_mcp_submit_to_client_warns_on_resubmit_and_other_clients(store):
    job, cid = _robotics(store)
    other = _agency(store, "Acme Payments", "Backend")
    store.add_to_pipeline(cid, other["id"], "Screened")
    server = build_server(store)
    res = call(server, "submit_to_client", candidate_id=cid, job_id=job["id"])
    d = data(res)
    assert d["moved"] and d["stage"] == "Submitted" and d["to"] == "dana@hfl.com" and "Lena Hoffmann" in d["writeup"]
    assert "nothing was sent" in text(res) and "Also in play at Acme Payments" in text(res)
    assert [a for a in store.ai_actions() if a["kind"] == "stage_move"]
    # Back to screened, then submitted again: the 12-month warning.
    store.move_stage(cid, job["id"], "Screened")
    res = call(server, "submit_to_client", candidate_id=cid, job_id=job["id"])
    assert "already submitted to Harbor Freight Labs" in text(res)
    nojob = store.add_job("Ops", company="Initech")
    store.add_to_pipeline(cid, nojob["id"])
    with pytest.raises(ToolError, match="no stage that counts as Submitted"):
        call(server, "submit_to_client", candidate_id=cid, job_id=nojob["id"])


def test_chase_list_per_manager_with_nudges_today_and_mark_chased(store):
    job, cid = _robotics(store)
    store.move_stage(cid, job["id"], "Submitted")
    _age(store, cid, job["id"], 8)
    fresh = store.add_candidate(name="Bo Li")["id"]
    store.add_to_pipeline(fresh, job["id"], "Submitted")  # 0 days: not waiting yet
    booked = store.add_candidate(name="Cy Park")["id"]
    store.add_to_pipeline(booked, job["id"], "Submitted")
    _age(store, booked, job["id"], 9)
    store.log(booked, "interview", "Client interview", at=_at(2), job_id=job["id"])  # booked: not waiting
    groups = chase.chase_list(store)
    assert len(groups) == 1 and [p["name"] for p in groups[0]["people"]] == ["Lena Hoffmann"]
    g = groups[0]
    assert g["manager"] == "Dana Reyes" and g["to"] == "dana@hfl.com"
    assert g["text"].startswith("Hi Dana,") and "Lena Hoffmann (Staff Robotics Engineer): Submitted since" in g["text"]
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "client_chase"]
    assert len(items) == 1 and items[0]["title"].startswith("Chase Dana Reyes at Harbor Freight Labs for feedback")
    c = local_client(store)
    assert "Client feedback" in c.get("/").text
    page = c.get("/clients/chase").text
    assert "Lena Hoffmann" in page and "mailto:dana@hfl.com" in page and "Bo Li" not in page
    r = c.post("/clients/harbor-freight-labs/chased", data={"manager": "dana reyes", "manager_name": "Dana Reyes"},
               follow_redirects=False)
    assert "Marked Dana Reyes as chased" in flash_of(r)
    assert not [i for i in actions.action_items(store)["items"] if i["kind"] == "client_chase"]
    res = call(build_server(store), "client_chase")
    assert "Nudge 1: Harbor Freight Labs, Dana Reyes (email dana@hfl.com)" in text(res)
    assert data(res)["nudges"][0]["count"] == 1
    c.post("/clients/chase/days", data={"days": "10"})
    assert chase.chase_days(store) == 10 and chase.chase_list(store) == []
    r = c.post("/clients/chase/days", data={"days": "zero"}, follow_redirects=False)
    assert "number from 1 to 90" in flash_of(r, "flash_error")


def test_the_update_lists_each_person_and_awaiting_feedback(store):
    job, cid = _robotics(store)
    store.move_stage(cid, job["id"], "Submitted")
    out = reports.report_text(reports.report(store, "harbor freight labs"))
    assert "Where each person stands:" in out
    line = next(l for l in out.splitlines() if l.startswith("• Lena Hoffmann"))
    assert "(Robotics Engineer at Fetch) for Staff Robotics Engineer: Submitted" in line
    assert line.endswith("awaiting your feedback")
    assert "Moved into Submitted 1" in out  # the counts stay


# ------------------------------------------------- 6. smaller send-out items
def test_note_to_the_client_opens_the_update_and_clears_on_send(store):
    job, cid = _robotics(store)
    c = local_client(store)
    r = c.post("/clients/harbor-freight-labs/note", data={"note": "Quiet week: two strong people in screens."},
               follow_redirects=False)
    assert "The note opens the update" in flash_of(r)
    out = reports.report_text(reports.report(store, "harbor freight labs"))
    assert out.splitlines()[2] == "Quiet week: two strong people in screens."
    reports.mark_sent(store, "harbor freight labs")
    assert reports.client_note(store, "harbor freight labs") == ""
    with pytest.raises(ValueError, match="too long"):
        reports.save_client_note(store, "harbor freight labs", "x" * 2000)


def test_mark_as_sent_before_the_day_says_so(store):
    _agency(store, "Acme", "Backend")
    today = datetime.now().astimezone().weekday()
    if today == 6:
        pytest.skip("no later day this week")
    later = reports.UPDATE_DAYS[today + 1]
    reports.save_client(store, "acme", update_day=later)
    assert f"Not due until {later}. Mark this update as sent anyway?" in local_client(store).get("/clients/acme").text


def test_clients_list_labels_its_counts(store):
    job = _agency(store, "Acme", "Backend")
    a = store.add_candidate(name="Ana")["id"]
    b = store.add_candidate(name="Bo")["id"]
    store.add_to_pipeline(a, job["id"])  # Sourced
    store.add_to_pipeline(b, job["id"], "Submitted")
    row = reports.overview(store)[0]
    assert row["in_play"] == 2 and row["presented"] == 1
    page = local_client(store).get("/clients").text
    assert "In play (incl. sourced)" in page and "1 in the update" in page


def test_undated_submittals_say_date_unknown_and_sort_last(store):
    job = _agency(store, "Acme", "Backend")
    a = store.add_candidate(name="Dated Person")["id"]
    b = store.add_candidate(name="Undated Person")["id"]
    store.add_to_pipeline(b, job["id"], "Submitted")
    with store._lock, store._conn:
        store._conn.execute("UPDATE timeline SET data = ? WHERE candidate_id = ? AND kind = 'stage'",
                            (json.dumps({"imported": True}), b))
    store.add_to_pipeline(a, job["id"], "Submitted")
    with store._lock, store._conn:
        store._conn.execute("UPDATE timeline SET at = ? WHERE candidate_id = ? AND kind = 'stage'", (_at(-30), a))
    subs = reports.submittals(store, "acme")
    assert [s["name"] for s in subs] == ["Dated Person", "Undated Person"] and subs[1]["undated"]
    assert "Date unknown (imported)" in local_client(store).get("/clients/acme").text


def test_new_pages_have_no_dashes(store):
    job, cid = _robotics(store)
    store.move_stage(cid, job["id"], "Submitted")
    _age(store, cid, job["id"], 8)
    c = local_client(store)
    iv = store.log(cid, "interview", "Chat", at=_at(20))
    for url in ("/clients", "/clients/chase", "/clients/harbor-freight-labs", "/clients/due",
                f"/jobs/{job['id']}/submit/{cid}", f"/interviews/{iv['id']}/job"):
        body = c.get(url).text
        assert "—" not in body and " – " not in body, url
