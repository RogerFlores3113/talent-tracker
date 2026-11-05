"""Cycle 5, fixer C: client updates. Unreviewed AI moves, MCP mark_update_sent and update_client, funnel counts
from stages entered, Counts as per stage, per-manager Mark as sent, close-outs as one line, the same-second move,
and the Friday send-out page (/clients/due, clients_due, Today)."""
import csv
import io
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import anyio
import pytest
from mcp.types import CallToolResult

from talent import actions, reports
from talent.mcp_server import build_server

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(result):
    content = result.content if isinstance(result, CallToolResult) else result[0]
    return "\n".join(c.text for c in content)


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _today():
    return reports.UPDATE_DAYS[datetime.now().astimezone().weekday()]


def _acme(store):
    be = store.add_job("Backend Engineer", company="Acme", stages=actions.stage_set("agency"))
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    maya = store.add_candidate(name="Maya Chen")["id"]
    store.add_to_pipeline(ana, be["id"], "Sourced")
    store.move_stage(ana, be["id"], "Submitted", by="app")
    store.add_to_pipeline(maya, be["id"], "Sourced")
    return be, ana, maya


def _ai_move(store, cid, job_id, stage):
    before = store._row("SELECT stage FROM applications WHERE candidate_id = ? AND job_id = ?", (cid, job_id))["stage"]
    store.move_stage(cid, job_id, stage, by="ai")
    store.mark_ai_stage(cid, job_id, before)
    return store._row("SELECT id FROM ai_actions ORDER BY id DESC LIMIT 1")["id"]


# ------------------------------------------------------ 1. unreviewed AI moves
def test_an_unreviewed_ai_move_is_flagged_for_the_recruiter_until_reviewed(store):
    be, ana, maya = _acme(store)
    aid = _ai_move(store, maya, be["id"], "Placed")
    rep = reports.report(store, "acme")
    assert [m["name"] for m in rep["ai_moves"]] == ["Maya Chen"]
    notes = reports.recruiter_notes(rep)
    assert any('"Maya Chen" to Placed (Backend Engineer)' in n and "haven't reviewed" in n for n in notes)
    assert "haven't reviewed" not in reports.report_text(rep)  # recruiter-only, never in the paste text
    c = local_client(store)
    body = c.get("/clients/acme").text
    assert "Review or undo on Changes" in body and "mark reviewed" in body
    form = body.split('action="/clients/acme/sent"')[1].split(">")[0]
    assert "not reviewed yet: Maya Chen to Placed" in form  # the Mark as sent confirm says so
    r = c.post("/clients/acme/ai-reviewed", data={"action_id": str(aid)}, follow_redirects=False)
    assert "Marked 1 AI move as reviewed" in flash_of(r)
    assert reports.report(store, "acme")["ai_moves"] == []
    assert "Review or undo on Changes" not in c.get("/clients/acme").text
    # An undone AI move isn't flagged either, and a recruiter id list can't mark anything else.
    aid2 = _ai_move(store, ana, be["id"], "Client interview")
    assert len(reports.report(store, "acme")["ai_moves"]) == 1
    store.undo_ai_action(aid2)
    assert reports.report(store, "acme")["ai_moves"] == []
    assert reports.mark_ai_reviewed(store, ["999", "x"]) == 0


# ------------------------------------------------------------ 2. MCP tools
def test_mark_update_sent_tool_records_it_and_changes_can_take_it_back(store):
    be, ana, maya = _acme(store)
    _ai_move(store, maya, be["id"], "Client interview")
    server = build_server(store)
    res = call(server, "mark_update_sent", client="ACME")
    assert res.content[0].text.startswith('Marked the update to "Acme" as sent')
    assert "hasn't reviewed" in text(res)  # it carried an AI move the recruiter hasn't seen
    sent = reports.updates(store, "acme")
    assert len(sent) == 1 and "Ana Ruiz to Submitted" in sent[0]["body"]
    a = store.ai_actions()[0]
    assert a["kind"] == "client_sent" and a["undoable"] and a["summary"] == 'Marked the update to "Acme" as sent'
    changes = local_client(store).get("/changes?who=ai").text
    assert 'href="/clients/acme"' in changes and "Marked the update to" in changes
    store.undo_ai_action(a["id"])
    assert reports.updates(store, "acme") == [] and reports.client(store, "acme")["last_update_sent_at"] is None
    assert store.ai_action(a["id"])["undone_at"]
    with pytest.raises(Exception, match="No client matches"):
        call(server, "mark_update_sent", client="Initech")


def test_taking_back_an_ai_mark_as_sent_in_the_app_marks_its_change_undone(store):
    _acme(store)
    call(build_server(store), "mark_update_sent", client="acme")
    uid = reports.updates(store, "acme")[0]["id"]
    local_client(store).post(f"/clients/acme/updates/{uid}/undo")
    a = store.ai_actions()[0]
    assert a["undone_at"] and not a["undoable"]


def test_update_client_tool_saves_logs_and_undoes(store):
    _acme(store)
    server = build_server(store)
    res = call(server, "update_client", client="acme", contacts="Pat <pat@acme.example>\nlee@acme.example",
               update_day="friday", time_zone="PT")
    first = res.content[0].text
    assert first.startswith('Changed "Acme":') and "update day set to 'Friday'" in first
    c = reports.client(store, "acme")
    assert c["update_day"] == "Friday" and c["time_zone"] == "America/Los_Angeles" and "lee@" in c["contacts"]
    a = store.ai_actions()[0]
    assert a["kind"] == "client_edit" and a["undoable"]
    assert 'Changed "Acme"\'s client details' in a["summary"]
    store.undo_ai_action(a["id"])
    c = reports.client(store, "acme")
    assert (c["update_day"], c["time_zone"], c["contacts"]) == (None, None, None)
    # One clear per call; a recruiter edit since blocks the undo.
    call(server, "update_client", client="acme", update_day="Monday", time_zone="ET")
    with pytest.raises(Exception, match="one field per call"):
        call(server, "update_client", client="acme", update_day="", time_zone="")
    reports.save_client(store, "acme", update_day="Tuesday")
    a = store.ai_actions()[0]
    assert not a["undoable"] and "changed again" in a["blocked"]
    with pytest.raises(Exception, match="Nothing to change"):
        call(server, "update_client", client="acme")


# ------------------------------------------------------- 3. funnel counts
def test_funnel_counts_only_steps_people_entered(store):
    be, ana, maya = _acme(store)
    store.move_stage(maya, be["id"], "Placed", by="app")  # straight from Sourced
    rep = reports.report(store, "acme")
    assert rep["reached"] == {"submitted": 1, "interview": 0, "offer": 0, "hired": 1}
    assert "Submitted or presented 1, Interviewed 0, Offer 0, Hired 1" in reports.report_text(rep)


# ----------------------------------------------------- 4. what a stage counts as
def test_stage_names_count_as_the_right_funnel_step():
    g = reports.stage_group
    assert [g(n) for n in ("Tech screen", "Takehome", "Take-home", "Cofounder chat", "Reference check", "Loop")] == [
        "interview"] * 6
    assert [g(n) for n in ("Phone screen", "Recruiter screen", "Intro call", "Intro chat")] == ["screening"] * 4
    assert g("Client Review") == "submitted" and g("Sourced") == "sourcing"


def test_counts_as_pick_per_stage_and_hiding_by_name(store):
    job = store.add_job("Founding Engineer", company="Loomwork",
                        stages=["Intro call", "Coding test", "Offer", "Hired", "Rejected"])
    bo = store.add_candidate(name="Bo Li")["id"]
    store.add_to_pipeline(bo, job["id"], "Intro call")
    store.move_stage(bo, job["id"], "Coding test", by="app")
    assert reports.report(store, "loomwork")["period"]["interview"] == 0  # "Coding test" reads as screening
    c = local_client(store)
    page = c.get(f"/jobs/{job['id']}/edit").text
    assert 'value="open:interview"' in page and "Interviewing" in page
    r = c.post(f"/jobs/{job['id']}/stages", data={"op": "outcome", "stage": "Coding test", "outcome": "open:interview"},
               follow_redirects=False)
    assert "Reports now count Coding test as Interviewing." in flash_of(r)
    rep = reports.report(store, "loomwork")
    assert rep["period"]["interview"] == 1 and rep["reached"]["interview"] == 1
    assert 'value="open:interview" selected' in c.get(f"/jobs/{job['id']}/edit").text
    store.rename_stage(job["id"], "Coding test", "Code exercise")  # the pick follows the stage, not its name
    assert reports.stage_groups(store, job["id"])["code exercise"] == "interview"
    c.post(f"/jobs/{job['id']}/stages", data={"op": "outcome", "stage": "Offer", "outcome": "lost"})
    assert store.resolve_stage(job["id"], "Offer")["outcome"] == "lost"
    with pytest.raises(ValueError):
        reports.set_stage_group(store, job["id"], "Hired", "nowhere")
    # Imported stages in first-seen order: Client Review before Submitted is still shown to the client.
    imp = store.add_job("SRE", company="Initech", stages=["Applied", "Client Review", "Phone screen", "Submitted",
                                                          "Hired", "Not a Fit"])
    assert actions.default_omit(store, imp["id"]) == ["Applied", "Phone screen"]


# ------------------------------------------------- 5. per manager, closed reqs
def _lumen(store):
    actions.set_inhouse(store, True)
    stages = actions.stage_set("inhouse")
    a = store.add_job("Data Scientist", company="Lumen Health", stages=stages, hiring_manager="Priya Raman",
                      hiring_manager_email="priya@lumen.example")
    b = store.add_job("Nurse Lead", company="Lumen Health", stages=stages, hiring_manager="Dana Cole")
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    bo = store.add_candidate(name="Aisha Bello")["id"]
    store.add_to_pipeline(ana, a["id"], "Applied")
    store.move_stage(ana, a["id"], "Interviews", by="app")
    store.add_to_pipeline(bo, b["id"], "Applied")
    with store._lock, store._conn:  # all of that happened two days ago
        store._conn.execute("UPDATE timeline SET at = ?", (_ago(2),))
    return a, b, ana, bo


def test_each_hiring_manager_is_marked_sent_on_their_own(store):
    a, b, ana, bo = _lumen(store)
    c = local_client(store)
    body = c.get("/clients/lumen-health").text
    assert body.count("Mark this one as sent") == 2 and "Mark all as sent" in body
    r = c.post("/clients/lumen-health/sent", data={"manager": "priya raman"}, follow_redirects=False)
    assert "Marked Priya Raman's update as sent" in flash_of(r)
    priya = reports.report(store, "lumen health", manager="priya raman")
    dana = reports.report(store, "lumen health", manager="dana cole")
    assert priya["range_from"] == "last_update" and priya["moves"] == []
    assert dana["range_from"] == "default" and [m["name"] for m in dana["moves"]] == ["Aisha Bello"]
    assert reports.client(store, "lumen health")["last_update_sent_at"] is None  # the company-wide one is apart
    reports.save_client(store, "lumen health", update_day=_today())
    assert reports.due_parts(store, "lumen health") == [("dana cole", "Dana Cole")]
    # Taking it back puts Priya's window where it was.
    uid = reports.updates(store, "lumen health")[0]["id"]
    reports.undo_sent(store, "lumen health", uid)
    assert reports.report(store, "lumen health", manager="priya raman")["range_from"] == "default"
    with pytest.raises(ValueError):
        reports.mark_sent(store, "lumen health", manager="nobody")


def test_a_closed_req_keeps_its_manager_until_hired_and_closed_goes_out(store):
    a, b, ana, bo = _lumen(store)
    store.move_stage(bo, b["id"], "Hired", by="app")
    store.update_job(b["id"], status="closed")
    assert [n for _, n in reports.managers(store, "lumen health")] == ["Dana Cole", "Priya Raman"]
    rep = reports.report(store, "lumen health", manager="dana cole")
    out = reports.report_text(rep)
    assert "Hired: Aisha Bello. Req closed: Nurse Lead." in out
    res = call(build_server(store), "client_report", client="lumen", hiring_manager="dana")
    assert "Req closed: Nurse Lead" in res.content[0].text
    reports.mark_sent(store, "lumen health", manager="dana cole")
    assert [n for _, n in reports.managers(store, "lumen health")] == ["Priya Raman"]


def test_a_close_out_is_one_line_in_the_update(store):
    be, ana, maya = _acme(store)
    store.update_job(be["id"], openings=1)
    store.move_stage(ana, be["id"], "Placed", by="app")
    for i in range(3):
        cid = store.add_candidate(name=f"Person {i}")["id"]
        store.add_to_pipeline(cid, be["id"], "Submitted")
    actions.close_out(store, be["id"], close_job=True)
    rep = reports.report(store, "acme")
    out = reports.report_text(rep)
    assert "4 people closed, position filled (Backend Engineer)" in out
    assert not [l for l in out.splitlines() if "Person 0 to" in l and "Submitted" not in l]
    assert "Ana Ruiz to Placed" in out
    assert "Placed: Ana Ruiz. Job closed: Backend Engineer." in out


# ------------------------------------------------------ 6. the same second
def test_a_move_in_the_same_second_as_mark_as_sent_lands_in_the_next_update(store):
    be, ana, maya = _acme(store)
    now = datetime.now(timezone.utc)
    reports.mark_sent(store, "acme", now=now)
    ts = now.replace(microsecond=0).isoformat()
    store.move_stage(maya, be["id"], "Submitted", by="app")
    with store._lock, store._conn:  # it happened in the very second the update was marked as sent
        store._conn.execute("UPDATE timeline SET at = ? WHERE id = (SELECT max(id) FROM timeline)", (ts,))
    rep = reports.report(store, "acme")
    assert [m["name"] for m in rep["moves"]] == ["Maya Chen"]
    sent = reports.updates(store, "acme")[0]["body"]
    assert "Maya Chen" not in sent and "Ana Ruiz to Submitted" in sent


# ------------------------------------------------------- 7. Friday send-out
def test_the_send_out_page_lists_due_updates_with_email_and_mark_as_sent(store):
    be, ana, maya = _acme(store)
    _ai_move(store, maya, be["id"], "Client interview")
    reports.save_client(store, "acme", update_day=_today(),
                        contacts="Dana <dana@acme.example>\nsam@acme.example\nDana again dana@acme.example")
    store.add_job("Designer", company="Globex")
    reports.save_client(store, "globex", update_day="Sunday" if _today() != "Sunday" else "Monday")
    c = local_client(store)
    body = c.get("/clients/due").text
    assert "Updates due this week" in body and "Due today" in body and "Acme" in body
    assert "Review or undo on Changes" in body
    href = body.split('href="mailto:')[1].split('"')[0]
    assert href.startswith("dana@acme.example?") and "cc=sam@acme.example" in unquote(href)
    assert "Email dana@acme.example (Cc sam@acme.example)" in body
    assert "—" not in body and "–" not in body
    later = reports.due_updates(store)["later"]
    if _today() != "Sunday":
        assert [d["name"] for d in later] == ["Globex"]
    # Today and list_action_items say it's due, and the Clients page links here.
    home = c.get("/").text
    assert "Client update due today: Acme" in home and 'href="/clients/due"' in home
    res = call(build_server(store), "list_action_items")
    assert "[update_due] Client update due today: Acme" in text(res)
    assert 'href="/clients/due"' in c.get("/clients").text
    # The MCP twin.
    res = call(build_server(store), "clients_due")
    first = res.content[0].text
    assert first.startswith("Update 1: Acme (due today). Email to dana@acme.example, cc sam@acme.example")
    u = res.structuredContent["updates"][0]
    assert u["cc"] == ["sam@acme.example"] and u["unreviewed_ai_moves"][0]["name"] == "Maya Chen"
    assert "For the recruiter only" in res.content[-1].text
    # Mark as sent from the page comes back to it, and the update is no longer due.
    r = c.post("/clients/acme/sent", data={"back": "due"}, follow_redirects=False)
    assert r.headers["location"].startswith("/clients/due") and "Marked the update to Acme as sent" in flash_of(r)
    assert reports.due_updates(store)["parts"] == []
    assert "Client update due" not in c.get("/").text
    assert "No client updates are due" in text(call(build_server(store), "clients_due"))


def test_a_long_update_opens_the_email_without_the_text():
    rep = {"manager": None, "email": "pat@acme.example", "client": {"name": "Acme", "contacts": "pat@acme.example"},
           "since": _ago(7), "until": _ago(0)}
    short = reports.email_link(rep, "short")
    assert not short["long"] and "&body=short" in short["href"]
    long = reports.email_link(rep, "x" * 3000)
    assert long["long"] and "body=" not in long["href"] and long["href"].startswith("mailto:pat@acme.example?subject=")


# ---------------------------------------------------------- small ones
def test_csv_leaves_out_people_who_are_out_unless_asked(store):
    be, ana, maya = _acme(store)
    store.move_stage(maya, be["id"], "Submitted", by="app")
    store.move_stage(maya, be["id"], "Client passed", by="app")
    c = local_client(store)
    names = {r["Candidate"] for r in csv.DictReader(io.StringIO(c.get("/clients/acme/pipeline.csv").text))}
    assert names == {"Ana Ruiz"}
    names = {r["Candidate"] for r in csv.DictReader(io.StringIO(c.get("/clients/acme/pipeline.csv?out=1").text))}
    assert names == {"Ana Ruiz", "Maya Chen"}


def test_date_range_is_said_once_and_interviews_show_their_session(store):
    be, ana, maya = _acme(store)
    now = datetime.now(timezone.utc)
    reports.mark_sent(store, "acme", now=now)
    rep = reports.report(store, "acme")
    t = reports.title(rep)
    assert " to " not in t and t.count(", 20") == 1
    out = reports.report_text(rep)
    assert "Changes since the last update." in out and "last update on" not in out
    when = (now + timedelta(days=2)).replace(microsecond=0).isoformat()
    store.log(ana, "interview", "Onsite: system design", at=when, job_id=be["id"], interviewer="Priya")
    store.log(ana, "interview", "Onsite: coding", at=when, job_id=be["id"], interviewer="Ravi")
    line = next(l for l in reports.report_text(reports.report(store, "acme")).splitlines() if "Ana Ruiz (Backend" in l)
    assert "Onsite: system design, with Priya" in line or "Onsite: coding, with Ravi" in line
    acme = next(r for r in reports.overview(store) if r["key"] == "acme")
    assert acme["interviews"] == 1  # one person's loop, not two sessions
    assert reports.span_text("2026-11-02T12:00:00+00:00", "2026-11-05T12:00:00+00:00", "UTC") == \
        "Mon Nov 2 to Thu Nov 5, 2026"


def test_with_no_contacts_the_update_goes_to_the_jobs_hiring_manager_email(store):
    job = store.add_job("SRE", company="Harbor", hiring_manager="Dana Fox", hiring_manager_email="dana@harbor.example")
    store.add_candidate(name="Lee Park")
    body = local_client(store).get("/clients/harbor").text
    assert 'href="mailto:dana@harbor.example?subject=' in body
    reports.save_client(store, "harbor", contacts="ops@harbor.example")
    assert 'href="mailto:ops@harbor.example?subject=' in local_client(store).get("/clients/harbor").text
    assert job["id"]
