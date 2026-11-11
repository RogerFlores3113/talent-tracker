"""Cycle 6 re-review, fixer G: an interview logged again with its job gets that job (or says it didn't), the
submittal write-up keeps app notes out, propose_candidates takes headline and notes, client tools, chase caps,
stage picks and an upper age limit, the write-up in submit_to_client's text, the early-send hint and the cc offer."""
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.types import CallToolResult

from talent import actions, chase, reports, review, shortlist
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


def _agency(store, company, title, manager=None, email=None):
    return store.add_job(title, company=company, stages=actions.stage_set("agency"),
                         **({"hiring_manager": manager} if manager else {}),
                         **({"hiring_manager_email": email} if email else {}))


def _age(store, cid, jid, days):
    with store._lock, store._conn:
        store._conn.execute("UPDATE applications SET stage_changed_at = ? WHERE candidate_id = ? AND job_id = ?",
                            (_at(-days), cid, jid))


def _job_of(store, iv_id):
    return store._row("SELECT job_id FROM timeline WHERE id = ?", (iv_id,))["job_id"]


def _lena(store):
    hfl = _agency(store, "Harbor Freight Labs", "Staff Robotics Engineer")
    acme = _agency(store, "Acme Payments", "Senior Backend Engineer")
    lena = store.add_candidate(name="Lena Hoffmann")["id"]
    store.add_to_pipeline(lena, hfl["id"], "Submitted")
    store.add_to_pipeline(lena, acme["id"])
    return hfl, acme, lena


# ------------------------------------------------- 1. log again with job_id sets the job
def test_logging_an_interview_again_with_job_id_sets_its_job_and_undo_takes_it_back(store):
    hfl, _acme, lena = _lena(store)
    server = build_server(store)
    when = _at(3)
    first = call(server, "log_activity", candidate_id=lena, kind="interview", summary="Client interview", when=when)
    iv = data(first)["logged"]["id"]
    assert _job_of(store, iv) is None
    again = call(server, "log_activity", candidate_id=lena, kind="interview", summary="Client interview", when=when,
                 job_id=hfl["id"])
    assert data(again)["logged"]["id"] == iv and _job_of(store, iv) == hfl["id"]
    assert "for Staff Robotics Engineer (Harbor Freight Labs)" in text(again)
    assert "its job is now Staff Robotics Engineer" in text(again)
    assert "Upcoming interviews" in reports.report_text(reports.report(store, "harbor freight labs"))
    act = next(a for a in store.ai_actions() if a["kind"] == "interview_job")
    assert act["undoable"] and "Set the job of an interview with Lena Hoffmann" in act["summary"]
    store.undo_ai_action(act["id"])
    assert _job_of(store, iv) is None


def test_logging_again_for_a_job_they_are_not_on_says_it_kept_no_job(store):
    _hfl, _acme, lena = _lena(store)
    other = _agency(store, "Initech", "Ops")
    server = build_server(store)
    when = _at(2)
    iv = data(call(server, "log_activity", candidate_id=lena, kind="interview", summary="Chat", when=when))["logged"]["id"]
    res = call(server, "log_activity", candidate_id=lena, kind="interview", summary="Chat", when=when, job_id=other["id"])
    assert _job_of(store, iv) is None
    assert "still has none" in text(res) and "for Ops" not in text(res)
    assert not [a for a in store.ai_actions() if a["kind"] == "interview_job"]


def test_logging_again_for_another_job_keeps_its_job_and_says_so(store):
    hfl, acme, lena = _lena(store)
    server = build_server(store)
    when = _at(2)
    iv = data(call(server, "log_activity", candidate_id=lena, kind="interview", summary="Chat", when=when,
                   job_id=hfl["id"]))["logged"]["id"]
    res = call(server, "log_activity", candidate_id=lena, kind="interview", summary="Chat", when=when,
               job_id=acme["id"])
    assert _job_of(store, iv) == hfl["id"]
    assert "stays there" in text(res) and "for Staff Robotics Engineer" in text(res)
    assert "for Senior Backend Engineer (Acme" not in text(res).split("\n")[0]


def test_cautious_keep_of_a_relog_sets_the_job(store):
    hfl, _acme, lena = _lena(store)
    when = _at(2)
    iv = store.log(lena, "interview", "Chat", at=when)["id"]
    review.set_cautious(store, True)
    server = build_server(store)
    res = call(server, "log_activity", candidate_id=lena, kind="interview", summary="Chat", when=when, job_id=hfl["id"])
    rid = data(res)["pending_review"]["id"]
    review.keep(store, rid)
    assert _job_of(store, iv) == hfl["id"]


# ------------------------------------------------- 2. the write-up's screen notes
def test_writeup_leaves_out_shortlist_notes_and_a_missing_title(store):
    job = _agency(store, "Harbor Freight Labs", "Staff Robotics Engineer", manager="Dana Reyes")
    cid = store.add_candidate(name="Marco Diaz", location="San Jose, CA")["id"]
    store.add_to_pipeline(cid, job["id"], "Screened", "Kept from the Shortlist.")
    body = chase.writeup(store, cid, job["id"])["body"]
    assert "Kept from the Shortlist" not in body and "From my screen" not in body
    assert "Based in: San Jose, CA" in body and "Now:" not in body
    other = store.add_candidate(name="Aisha Khan", current_title="Controls Engineer")["id"]
    store.add_to_pipeline(other, job["id"], "Screened", "Kept from the Shortlist. Why they fit: 6 years of ROS")
    store.log(other, "note", "Great call. Expecting $180k base.", job_id=job["id"])
    body = chase.writeup(store, other, job["id"], include_notes=True)["body"]  # notes are opt-in (cycle 18)
    assert "- 6 years of ROS" in body and "- Great call" in body and "Kept from" not in body
    assert "Now: Controls Engineer" in body and "Comp: Expecting $180k base" in body
    assert chase.screen_note("Sales Navigator link (it only opens inside Sales Navigator, so x): y") is None


# ------------------------------------------------- 3. propose_candidates field names
def test_propose_candidates_takes_headline_and_notes_and_names_ignored_fields(store):
    job = _agency(store, "Harbor Freight Labs", "Staff Robotics Engineer")
    res = call(build_server(store), "propose_candidates", job_id=job["id"], people=[
        {"name": "Marco Diaz", "linkedin_url": "https://www.linkedin.com/in/marco-diaz", "headline": "Robotics Lead",
         "notes": "Shipped two AMR fleets", "salary": "200k"}])
    row = shortlist.listed(store, job["id"], "proposed")[0]
    assert row["title"] == "Robotics Lead" and row["fit"] == "Shipped two AMR fleets"
    assert "Not saved" in text(res) and "salary" in text(res) and data(res)["ignored_fields"] == ["salary"]
    plain = call(build_server(store), "propose_candidates", job_id=job["id"], people=[
        {"name": "Bo Li", "linkedin_url": "https://www.linkedin.com/in/bo-li", "title": "SWE", "fit": "Fits"}])
    assert "Not saved" not in text(plain)


# ------------------------------------------------- 4. client tools
def test_create_client_says_undo_once_and_update_client_takes_name(store):
    server = build_server(store)
    res = call(server, "create_client", name="Harbor Freight Labs", update_day="fri")
    assert text(res).count("can undo it on Changes") == 1
    res = call(server, "update_client", name="Harbor Freight Labs", notes="Prefers short updates")
    assert reports.client(store, "harbor freight labs")["notes"] == "Prefers short updates"
    assert "Changed" in text(res)


# ------------------------------------------------- 5. caps and the write-up in text
def _many(store, n, stage="Submitted", days=10):
    job = _agency(store, "Harbor Freight Labs", "Staff Robotics Engineer", manager="Dana Reyes", email="dana@hfl.com")
    for i in range(n):
        cid = store.add_candidate(name=f"Person {i:02d}")["id"]
        store.add_to_pipeline(cid, job["id"], stage)
        _age(store, cid, job["id"], days + i)
    return job


def test_chase_page_and_tool_cap_each_manager(store):
    _many(store, 30)
    c = local_client(store)
    page = c.get("/clients/chase").text
    assert "Show all 30" in page and page.count('<td><a href="/candidates/') == 25
    anchor = "chase-harbor-freight-labs-dana-reyes"
    full = c.get(f"/clients/chase?all={anchor}").text
    assert full.count('<td><a href="/candidates/') == 30 and "Show fewer" in full
    d = data(call(build_server(store), "client_chase"))
    g = d["nudges"][0]
    assert g["count"] == 30 and g["shown"] == 25 and len(g["people"]) == 25 and d["nudge_count"] == 1


def test_submit_to_client_puts_the_writeup_in_its_text(store):
    job = _agency(store, "Harbor Freight Labs", "Staff Robotics Engineer", manager="Dana Reyes", email="dana@hfl.com")
    cid = store.add_candidate(name="Lena Hoffmann", current_title="Robotics Engineer")["id"]
    store.add_to_pipeline(cid, job["id"], "Screened")
    res = call(build_server(store), "submit_to_client", candidate_id=cid, job_id=job["id"])
    t = text(res)
    assert "Subject: Lena Hoffmann for Staff Robotics Engineer" in t and "To: dana@hfl.com" in t
    assert "I'd like to put Lena Hoffmann forward" in t and "nothing was sent" in t


# ------------------------------------------------- 6. stage picks and the upper age limit
def test_onsite_is_left_out_by_default_and_can_be_counted(store):
    job = store.add_job("Backend", company="Acme", stages=["Sourced", "Submitted", "Onsite", "Reference Check",
                                                           "Offer", "Hired"])
    a = store.add_candidate(name="Ana Ruiz")["id"]
    b = store.add_candidate(name="Bo Li")["id"]
    store.add_to_pipeline(a, job["id"], "Submitted")
    store.add_to_pipeline(b, job["id"], "Onsite")
    _age(store, a, job["id"], 8)
    _age(store, b, job["id"], 8)
    left = {}
    names = [p["name"] for g in chase.chase_list(store, left_out=left) for p in g["people"]]
    assert names == ["Ana Ruiz"] and left["stages"] == 1
    choices = {s["name"]: s["counted"] for s in chase.stage_choices(store)}
    assert choices.get("Onsite") is False and choices.get("Submitted") is True
    c = local_client(store)
    assert "Stages the chase counts" in c.get("/clients/chase").text
    r = c.post("/clients/chase/stages", data={"shown": ["Submitted", "Onsite"], "count": ["Submitted", "Onsite"]},
               follow_redirects=False)
    assert "Saved which stages" in flash_of(r)
    assert sorted(p["name"] for g in chase.chase_list(store) for p in g["people"]) == ["Ana Ruiz", "Bo Li"]
    c.post("/clients/chase/stages", data={"shown": ["Submitted", "Onsite"], "count": ["Onsite"]})
    assert [p["name"] for g in chase.chase_list(store) for p in g["people"]] == ["Bo Li"]


def test_an_upper_age_limit_leaves_very_old_rows_out(store):
    _many(store, 3, days=100)  # 100, 101 and 102 days
    job = store.add_job("Ops", company="Harbor Freight Labs", stages=actions.stage_set("agency"))
    fresh = store.add_candidate(name="Fresh Face")["id"]
    store.add_to_pipeline(fresh, job["id"], "Submitted")
    _age(store, fresh, job["id"], 7)
    c = local_client(store)
    r = c.post("/clients/chase/days", data={"days": "5", "max_days": "60"}, follow_redirects=False)
    assert "5 to 60 days" in flash_of(r) and chase.chase_max_days(store) == 60
    left = {}
    assert [p["name"] for g in chase.chase_list(store, left_out=left) for p in g["people"]] == ["Fresh Face"]
    assert left["too_old"] == 3
    assert "3 waiting more than 60 days" in c.get("/clients/chase").text
    assert "3 waiting more than 60 days" in text(call(build_server(store), "client_chase"))
    r = c.post("/clients/chase/days", data={"days": "5", "max_days": "3"}, follow_redirects=False)
    assert "more than 5 days" in flash_of(r, "flash_error")
    c.post("/clients/chase/days", data={"days": "5", "max_days": ""})
    assert chase.chase_max_days(store) is None
    assert sum(len(g["people"]) for g in chase.chase_list(store)) == 4


# ------------------------------------------------- 7. early send hint and the cc offer
def test_mark_update_sent_early_says_not_due_yet(store):
    _agency(store, "Acme", "Backend")
    today = reports.as_local(datetime.now(timezone.utc)).weekday()
    if today == 6:
        pytest.skip("no later day this week")
    later = reports.UPDATE_DAYS[today + 1]
    reports.save_client(store, "acme", update_day=later)
    res = call(build_server(store), "mark_update_sent", client="acme")
    assert f"Not due until {later}" in text(res)


def test_per_manager_agency_update_offers_the_main_contact_on_cc(store):
    _agency(store, "Acme Payments", "Backend", manager="Dev Patel", email="dev@acme.com")
    _agency(store, "Acme Payments", "Frontend", manager="Priya Shah", email="priya@acme.com")
    reports.save_client(store, "acme payments", contacts="Jordan <jordan@acme.com>")
    page = local_client(store).get("/clients/acme-payments").text
    assert "Or with jordan@acme.com on Cc" in page and "&amp;cc=jordan@acme.com" in page


def test_new_copy_has_no_dashes(store):
    _many(store, 30)
    c = local_client(store)
    for url in ("/clients/chase", "/clients/chase?all=chase-harbor-freight-labs-dana-reyes", "/clients/harbor-freight-labs"):
        body = c.get(url).text
        assert "—" not in body and " – " not in body, url
