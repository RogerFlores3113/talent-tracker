"""Cycle 4 re-review, fixer V: client reports that are safe to send."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import anyio
from mcp.types import CallToolResult

from conftest import local_client

from talent import actions, importers, reports
from talent.mcp_server import HIDDEN_LEAD, build_server


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    res = anyio.run(go)
    return res if isinstance(res, CallToolResult) else CallToolResult(content=res[0], structuredContent=res[1])


def _iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _soon(days=2):
    return _iso((datetime.now(timezone.utc) + timedelta(days=days)).replace(hour=17, minute=0, second=0))


def _pre(body, i=0):
    """The text inside the i-th paste block on a client page."""
    return body.split('<pre class="code wrap" id="report-text')[i + 1].split("</pre>")[0]


def _agency(store):
    be = store.add_job("Backend Engineer", company="Acme", stages=actions.stage_set("agency"))
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    bo = store.add_candidate(name="Bo Li")["id"]
    store.add_to_pipeline(ana, be["id"], "Sourced")
    store.move_stage(ana, be["id"], "Submitted", by="app")
    store.add_to_pipeline(bo, be["id"], "Sourced")  # internal
    return be, ana, bo


# ------------------------------------------------- 1. hidden count is the recruiter's
def test_hidden_count_is_never_in_the_paste_text_copy_or_mailto(store):
    _agency(store)
    reports.save_client(store, "acme", contacts="pat@acme.example")
    rep = reports.report(store, "acme")
    out = reports.report_text(rep)
    assert "Not shown" not in out and "Bo Li" not in out
    assert reports.recruiter_notes(rep) == ["Not shown: Sourced (1)"]
    body = local_client(store).get("/clients/acme").text
    assert "Not shown" not in _pre(body)
    mailto = body.split('href="mailto:')[1].split('"')[0]
    assert "Not shown" not in unquote(mailto) and "Sourced" not in unquote(mailto)
    assert "For you only, not part of the update: Not shown: Sourced (1)." in body
    res = call(build_server(store), "client_report", client="acme")
    assert "Not shown" not in res.content[0].text
    assert res.content[-1].text.endswith(f"{HIDDEN_LEAD} Not shown: Sourced (1).")
    assert res.structuredContent["left_out"] == {"Sourced": 1}
    sent = reports.mark_sent(store, "acme")
    assert "Not shown" not in sent["body"]


# ------------------------------------------------------ 2. people, not applications
def test_one_person_on_two_jobs_and_back_and_forth_counts_once(store):
    ml = store.add_job("ML Engineer", company="Harbor", stages=actions.stage_set("agency"))
    sb = store.add_job("Staff Backend Engineer", company="Harbor", stages=actions.stage_set("agency"))
    pr = store.add_candidate(name="Priya Raman")["id"]
    store.add_to_pipeline(pr, ml["id"], "Sourced")
    store.move_stage(pr, ml["id"], "Submitted", by="app")
    store.move_stage(pr, ml["id"], "Sourced", by="app")
    store.move_stage(pr, ml["id"], "Submitted", by="app")
    store.add_to_pipeline(pr, sb["id"], "Submitted")
    rep = reports.report(store, "harbor")
    assert rep["in_play"] == 1 and rep["stand"]["submitted"] == 1 and rep["period"]["submitted"] == 1
    assert rep["reached"]["submitted"] == 1
    out = reports.report_text(rep)
    assert "In process now: 1" in out and "- Submitted: 1" in out
    assert "Moved into Submitted 1," in out and "Submitted or presented 1" in out
    assert out.count("Priya Raman to Submitted (ML Engineer)") == 1
    assert out.count("Priya Raman to Submitted (Staff Backend Engineer)") == 1


# ---------------------------------------------------------- 3. in-house, per manager
def _lumen(store):
    actions.set_inhouse(store, True)
    stages = actions.stage_set("inhouse")
    a = store.add_job("Data Scientist", company="Lumen Health", stages=stages, hiring_manager="Priya Raman",
                      hiring_manager_email="priya@lumen.example")
    b = store.add_job("Nurse Lead", company="Lumen Health", stages=stages, hiring_manager="Dana Cole")
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    bo = store.add_candidate(name="Bo Li")["id"]
    store.add_to_pipeline(ana, a["id"], "Applied")
    store.move_stage(ana, a["id"], "Interviews", by="app")
    store.add_to_pipeline(bo, b["id"], "Applied")
    store.move_stage(bo, b["id"], "Offer", by="app")
    reports.save_client(store, "lumen health", contacts="HR team <hr@lumen.example>")
    return a, b, ana, bo


def test_inhouse_report_is_split_by_hiring_manager_with_their_own_email(store):
    _lumen(store)
    assert [n for _, n in reports.managers(store, "lumen health")] == ["Dana Cole", "Priya Raman"]
    body = local_client(store).get("/clients/lumen-health").text
    dana, priya = _pre(body, 0), _pre(body, 1)
    assert "hiring update for Dana Cole" in dana and "Bo Li" in dana and "Ana Ruiz" not in dana
    assert "hiring update for Priya Raman" in priya and "Ana Ruiz" in priya and "Bo Li" not in priya
    assert 'data-copy-from="report-text"' in body and 'data-copy-from="report-text-2"' in body
    assert 'href="mailto:priya@lumen.example?' in body and 'href="mailto:hr@lumen.example?' in body
    for text in (dana, priya):
        assert "Submit" not in text and "presented" not in text
        assert "Moved into Interviewing" in text
    assert 'id="submittals"' not in body and "Submittals" not in body
    sent = reports.mark_sent(store, "lumen health")
    assert "Dana Cole" in sent["body"] and "Priya Raman" in sent["body"]
    csv_text = local_client(store).get("/clients/lumen-health/pipeline.csv").text
    assert "Last submitted" not in csv_text.splitlines()[0]


def test_inhouse_client_report_tool_gives_one_block_per_manager_or_just_one(store):
    _lumen(store)
    server = build_server(store)
    res = call(server, "client_report", client="lumen")
    texts = [c.text for c in res.content]
    assert len(texts) == 3 and "Dana Cole" in texts[0] and "Priya Raman" in texts[1]
    assert "paste each only to that manager" in texts[2]
    assert {m["hiring_manager"]: m["email"] for m in res.structuredContent["hiring_managers"]} == {
        "Dana Cole": "hr@lumen.example", "Priya Raman": "priya@lumen.example"}
    one = call(server, "client_report", client="lumen", hiring_manager="priya")
    assert "Ana Ruiz" in one.content[0].text and "Bo Li" not in one.content[0].text
    assert "Paste only the first block" in one.content[-1].text
    assert one.structuredContent["in_play"] == 1


def test_agency_report_keeps_one_update_and_its_words(store):
    store.add_job("Backend", company="Acme", hiring_manager="Lee Park", hiring_manager_email="lee@acme.example")
    _agency(store)
    assert reports.managers(store, "acme") == []
    body = local_client(store).get("/clients/acme").text
    assert body.count('<pre class="code wrap" id="report-text') == 1
    assert "Moved into Submitted" in _pre(body) and 'id="submittals"' in body


# --------------------------------------------------------------- 4. overfilled jobs
def test_more_hires_than_openings_is_said_and_flagged(store):
    dl = store.add_job("Data Lead", company="Initech", openings=1)
    for n in ("Hana Brandt", "Daniel Reyes"):
        cid = store.add_candidate(name=n)["id"]
        store.add_to_pipeline(cid, dl["id"], "Hired")
    rep = reports.report(store, "initech")
    out = reports.report_text(rep)
    assert "2 hired, 1 opening (over by 1)" in out and "1 of 1 opening filled" not in out
    assert rep["overfilled"] == [{"job_id": dl["id"], "title": "Data Lead", "hired": 2, "openings": 1, "over": 1}]
    body = local_client(store).get("/clients/initech").text
    assert 'class="flash warn no-print" role="alert"' in body and "has 2 hired for 1 opening (over by 1)" in body
    store.add_job("Analyst", company="Initech")
    out = reports.report_text(reports.report(store, "initech"))
    assert "2 hired, 2 openings (Data Lead over by 1)" in out and "1 of 2 openings filled" not in out
    res = call(build_server(store), "client_report", client="initech")
    assert res.structuredContent["overfilled"][0]["over"] == 1 and "over by 1" in res.content[-1].text


# ------------------------------------------------------ 5. booked interviews show
def test_a_booked_interview_shows_though_the_person_is_still_in_sourced(store):
    be, ana, bo = _agency(store)
    store.log(bo, "interview", "HM screen", at=_soon(), job_id=be["id"])
    rep = reports.report(store, "acme")
    assert [u["name"] for u in rep["upcoming"]] == ["Bo Li"]
    assert rep["not_shown"] == {} and rep["in_play"] == 1  # counted nowhere, named only with the interview
    out = reports.report_text(rep)
    assert "Upcoming interviews:" in out and "Bo Li (Backend Engineer)" in out and "to Sourced" not in out
    assert any("Bo Li (Sourced, Backend Engineer)" in n for n in reports.recruiter_notes(rep))
    # status_digest and job_pipeline list them too, and count them.
    digest = actions.status_digest(store, "acme")
    assert "Sourced (1): Bo Li" in digest and "Next interviews:" in digest and "- Bo Li:" in digest
    assert "2 candidates in process" in digest
    note = actions.hidden_note(store, be["id"])
    assert "Not shown" not in note and "interview is booked: Bo Li (Sourced)" in note
    server = build_server(store)
    res = call(server, "status_digest", company="acme")
    assert "Bo Li" in res.content[0].text and "interview is booked" in res.content[1].text
    assert "Bo Li" in call(server, "job_pipeline", job_id=be["id"]).content[0].text


def test_without_a_booking_sourced_people_stay_out_of_the_digest(store):
    be, ana, bo = _agency(store)
    digest = actions.status_digest(store, "acme")
    assert "Bo Li" not in digest
    assert actions.hidden_note(store, be["id"]) == "Not shown: Sourced (1)"


# ------------------------------------------------------- 6. imports aren't moves
def test_an_import_is_not_this_periods_moves(store):
    store.add_job("Engineer", company="Initech")
    text = ("Name,Email,Job,Client,Status\n"
            "Ana,ana@x.com,Engineer,Initech,Hired\n"
            "Bo,bo@x.com,Engineer,Initech,Rejected\n"
            "Cy,cy@x.com,Engineer,Initech,Offer\n")
    header, rows = importers.read_csv(text)
    importers.import_rows(store, header, rows, importers.guess_mapping(header, rows), add_stages=True, backup=False)
    rep = reports.report(store, "initech")
    assert rep["moves"] == [] and rep["imported_moves"] == 3
    assert all(rep["period"][g] == 0 for g in rep["period"])
    out = reports.report_text(rep)
    assert "Moves this period: none." in out and "Hired 0, Out 0" in out
    assert rep["stand"]["offer"] == 1  # where people stand now still counts


# ------------------------------------------- 7. stage names, not their place
def test_offer_onsite_and_hm_screen_are_never_hidden_for_their_place(store):
    job = store.add_job("Sales Engineer", company="Initech",
                        stages=["Sourced", "Screened", "HM Screen", "Onsite", "Offer", "Client Submitted", "Hired",
                                "Rejected"])
    assert actions.default_omit(store, job["id"]) == ["Sourced", "Screened"]
    people = {}
    for n, stage in (("Xavi Reyes", "Screened"), ("Hana Brandt", "HM Screen"), ("Ola Berg", "Onsite")):
        people[n] = store.add_candidate(name=n)["id"]
        store.add_to_pipeline(people[n], job["id"], stage)
    store.move_stage(people["Xavi Reyes"], job["id"], "Offer", by="ai")
    rep = reports.report(store, "initech")
    assert rep["not_shown"] == {}
    assert rep["stand"]["offer"] == 1 and rep["stand"]["interview"] == 2 and rep["period"]["offer"] == 1
    assert "Xavi Reyes to Offer (Sales Engineer)" in reports.report_text(rep)
    # Even a saved Leave out choice can't hide a stage the client has seen from the client report.
    actions.save_omit(store, job["id"], ["Sourced", "Offer"])
    assert reports.report(store, "initech")["stand"]["offer"] == 1
    assert reports.stage_group("HM Screen") == "interview" and reports.stage_group("Onsite") == "interview"


def test_client_pages_have_no_dashes(store):
    _lumen(store)
    dl = store.add_job("Data Lead", company="Initech", openings=1)
    for n in ("Hana Brandt", "Daniel Reyes"):
        store.add_to_pipeline(store.add_candidate(name=n)["id"], dl["id"], "Hired")
    c = local_client(store)
    for path in ("/clients/lumen-health", "/clients/initech"):
        body = c.get(path).text
        assert "—" not in body and "–" not in body, path
