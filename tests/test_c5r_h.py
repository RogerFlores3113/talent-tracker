"""Cycle 5 re-review, fixer H: client updates hold back unreviewed AI moves into hired or out, and the send-out
polish (later-this-week drafts, interview results and loops, openings, close-outs, time zones, titles, labels,
a closed manager's last copy, shorter email links)."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import anyio
import pytest
from mcp.types import CallToolResult

from talent import actions, reports
from talent.mcp_server import build_server
from talent.store import parse_when, set_shown_zone, shown_zone, to_dt

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(result):
    content = result.content if isinstance(result, CallToolResult) else result[0]
    return "\n".join(c.text for c in content)


def _ago(days=0, hours=0):
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours)).replace(microsecond=0).isoformat()


def _weekday(offset=0):
    return reports.UPDATE_DAYS[(datetime.now().astimezone().weekday() + offset) % 7]


def _acme(store):
    be = store.add_job("Backend Engineer", company="Acme", stages=actions.stage_set("agency"))
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    maya = store.add_candidate(name="Maya Chen")["id"]
    store.add_to_pipeline(ana, be["id"], "Sourced")
    store.move_stage(ana, be["id"], "Submitted", by="app")
    store.add_to_pipeline(maya, be["id"], "Sourced")
    store.move_stage(maya, be["id"], "Submitted", by="app")
    return be, ana, maya


def _ai_move(store, cid, job_id, stage):
    before = store._row("SELECT stage FROM applications WHERE candidate_id = ? AND job_id = ?", (cid, job_id))["stage"]
    store.move_stage(cid, job_id, stage, by="ai")
    store.mark_ai_stage(cid, job_id, before)
    return store._row("SELECT id FROM ai_actions ORDER BY id DESC LIMIT 1")["id"]


# ------------------------------------------------- 1. held back until reviewed
def test_an_unreviewed_ai_hire_stays_out_of_the_text_copy_and_email_until_reviewed(store):
    be, ana, maya = _acme(store)
    reports.save_client(store, "acme", contacts="pat@acme.example")
    aid = _ai_move(store, maya, be["id"], "Placed")
    _ai_move(store, ana, be["id"], "Client interview")  # not a hire or a no: it stays in, flagged
    rep = reports.report(store, "acme")
    out = reports.report_text(rep)
    assert "Maya Chen to Placed" not in out and "Ana Ruiz to Client interview" in out
    assert "Placed 0" in out and "0 of 1 opening filled" in out and "In process now: 2" in out
    assert [m["name"] for m in rep["held"]] == ["Maya Chen"]
    assert {m["name"] for m in rep["ai_moves"]} == {"Maya Chen", "Ana Ruiz"}
    notes = reports.recruiter_notes(rep)
    assert notes[0].startswith('(1 held back, review first) "Maya Chen" to Placed (Backend Engineer)')
    link = reports.email_link(rep, out)
    assert "Maya Chen to Placed" not in unquote(link["href"]) and "Maya Chen to Submitted" in unquote(link["href"])
    c = local_client(store)
    body = c.get("/clients/acme").text
    pre = body.split('id="report-text">')[1].split("</pre>")[0]
    assert "Maya Chen to Placed" not in pre and "(1 held back, review first)" in body
    # Once reviewed, it's part of the update.
    reports.mark_ai_reviewed(store, [aid])
    out = reports.report_text(reports.report(store, "acme"))
    assert "Maya Chen to Placed" in out and "1 of 1 opening filled" in out


def test_an_ai_move_out_is_held_and_undoing_it_drops_the_hold(store):
    be, ana, maya = _acme(store)
    aid = _ai_move(store, ana, be["id"], "Client passed")
    rep = reports.report(store, "acme")
    assert "Ana Ruiz to Client passed" not in reports.report_text(rep) and rep["held"]
    assert rep["stand"]["submitted"] == 2  # she reads as still Submitted
    store.undo_ai_action(aid)
    rep = reports.report(store, "acme")
    assert rep["held"] == [] and rep["ai_moves"] == []


def test_mark_update_sent_refuses_while_moves_are_unreviewed_unless_confirmed(store):
    be, ana, maya = _acme(store)
    aid = _ai_move(store, maya, be["id"], "Placed")
    server = build_server(store)
    with pytest.raises(Exception, match=r"Not marked as sent.*\"Maya Chen\" to Placed.*held back.*confirm=true"):
        call(server, "mark_update_sent", client="acme")
    assert reports.updates(store, "acme") == []
    res = call(server, "mark_update_sent", client="acme", confirm=True)
    assert "Marked anyway" in text(res) and "left out of the text" in text(res)
    assert "Maya Chen to Placed" not in reports.updates(store, "acme")[0]["body"]
    store.undo_ai_action(store.ai_actions()[0]["id"])  # take back the AI's mark as sent
    reports.mark_ai_reviewed(store, [aid])
    res = call(server, "mark_update_sent", client="acme")  # nothing unreviewed: no confirm needed
    assert res.content[0].text.startswith('Marked the update to "Acme" as sent') and "warning" not in res.structuredContent


def test_the_web_mark_as_sent_stops_on_an_ai_move_the_page_did_not_show(store):
    be, ana, maya = _acme(store)
    first = _ai_move(store, maya, be["id"], "Placed")
    c = local_client(store)
    body = c.get("/clients/acme").text
    assert f'name="ai_seen" value="{first}"' in body
    _ai_move(store, ana, be["id"], "Client interview")  # made after the page loaded
    r = c.post("/clients/acme/sent", data={"ai_seen": str(first)}, follow_redirects=False)
    assert "Not marked as sent" in flash_of(r, "flash_error") and '"Ana Ruiz" to Client interview' in flash_of(
        r, "flash_error")
    seen = c.get("/clients/acme").text.split('name="ai_seen" value="')[-1].split('"')[0]
    r = c.post("/clients/acme/sent", data={"ai_seen": seen}, follow_redirects=False)
    assert "Marked as sent" in flash_of(r)


# --------------------------------------------------- 2. later this week drafts
def test_send_out_shows_the_draft_for_updates_due_later_this_week(store):
    if datetime.now().astimezone().weekday() == 6:
        pytest.skip("no later day this week on a Sunday")
    _acme(store)
    reports.save_client(store, "acme", update_day=_weekday(1))
    due = reports.due_updates(store)
    assert due["parts"] == [] and [p["name"] for p in due["later_parts"]] == ["Acme"]
    body = local_client(store).get("/clients/due").text
    later = body.split('id="later"')[1]
    assert "Later this week" in body and "Acme: hiring update" in later and "Ana Ruiz to Submitted" in later
    assert "Copy the draft" in later and "Mark as sent" not in later
    res = call(build_server(store), "clients_due")
    assert "Draft 1, due " in text(res) and "Ana Ruiz to Submitted" in text(res)
    assert res.structuredContent["drafts_later_this_week"][0]["client"] == "Acme"


# ------------------------------------------- 3. interview results and loops
def _lumen(store):
    actions.set_inhouse(store, True)
    j = store.add_job("Data Scientist", company="Lumen Health", stages=actions.stage_set("inhouse"),
                      hiring_manager="Priya Natarajan", hiring_manager_email="priya@lumen.example")
    mei = store.add_candidate(name="Mei Lin Zhao")["id"]
    jordan = store.add_candidate(name="Jordan Ellis")["id"]
    store.add_to_pipeline(mei, j["id"], "Hiring manager screen")
    store.add_to_pipeline(jordan, j["id"], "Interviews")
    return j, mei, jordan


def test_the_manager_update_has_interview_results_and_a_loop_on_one_line(store):
    j, mei, jordan = _lumen(store)
    iv = store.log(mei, "interview", "Hiring manager screen", at=_ago(hours=5), job_id=j["id"])
    store.update_interview(iv["id"], outcome="passed")
    day = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=15, minute=0, second=0, microsecond=0)
    for i, what in enumerate(("Onsite: system design", "Onsite: coding", "Onsite: values")):
        store.log(jordan, "interview", what, at=(day + timedelta(hours=i)).isoformat(), job_id=j["id"])
    moved = store.log(jordan, "interview", "Onsite: hiring manager", at=(day + timedelta(hours=4)).isoformat(),
                      job_id=j["id"])
    store.update_interview(moved["id"], at=(day + timedelta(days=2)).isoformat())  # one session moved later
    rep = reports.report(store, "lumen health", manager="priya natarajan")
    out = reports.report_text(rep)
    assert "Interview results:\n- Mei Lin Zhao passed Hiring manager screen (" in out
    loop = [line for line in out.splitlines() if line.startswith("- Jordan Ellis")]
    assert len(loop) == 1 and loop[0].startswith("- Jordan Ellis (Data Scientist): onsite, 4 sessions, ")
    assert " and " in loop[0]  # two days
    res = call(build_server(store), "client_report", client="lumen", hiring_manager="priya")
    assert "Mei Lin Zhao passed Hiring manager screen" in res.content[0].text
    assert out.startswith("Hiring update, Lumen Health, ")  # in-house: no "for Priya" sent to Priya


# ----------------------------------------------------- 4. openings and close-out
def test_openings_an_import_guessed_never_read_as_n_of_n_filled(store):
    be, ana, maya = _acme(store)
    store.move_stage(maya, be["id"], "Placed", by="app")
    store.update_job(be["id"], openings=1)
    actions._put_setting(store, "import_openings_ask", json.dumps([be["id"]]))
    rep = reports.report(store, "acme")
    out = reports.report_text(rep)
    assert "of 1 opening filled" not in out and "1 placed so far" in out
    assert any("Openings not set yet for Backend Engineer" in n for n in reports.recruiter_notes(rep))


def test_a_close_out_is_not_counted_as_out(store):
    be, ana, maya = _acme(store)
    store.move_stage(maya, be["id"], "Placed", by="app")
    for i in range(3):
        cid = store.add_candidate(name=f"Person {i}")["id"]
        store.add_to_pipeline(cid, be["id"], "Submitted")
    actions.close_out(store, be["id"], close_job=False)
    rep = reports.report(store, "acme")
    assert rep["period"]["out"] == 0 and "4 people closed, position filled" in reports.report_text(rep)


def test_the_job_page_says_why_still_in_play_differs_from_in_process(store):
    be, ana, maya = _acme(store)
    store.move_stage(maya, be["id"], "Placed", by="app")
    cid = store.add_candidate(name="Sam Sourced")["id"]
    store.add_to_pipeline(cid, be["id"], "Sourced")
    body = local_client(store).get(f"/jobs/{be['id']}").text
    assert "2 people are still in play" in body and "The status update says 1 in process" in body


# ------------------------------------------------------------ 5. time zones
def test_the_recruiters_own_zone_shows_times_and_reads_typed_ones(store):
    try:
        reports.save_user_zone(store, "CT")
        assert reports.user_zone(store) == "America/Chicago"
        assert actions.local_time("2026-11-06T20:00:00+00:00") == "Fri Nov 6, 2:00 PM"
        assert parse_when("2026-11-06 14:00") == "2026-11-06T20:00:00+00:00"
        c = local_client(store)
        assert "America/Chicago" in c.get("/settings").text
        r = c.post("/settings/time-zone", data={"time_zone": ""}, follow_redirects=False)
        assert "this computer's time zone" in flash_of(r) and shown_zone() is None
        r = c.post("/settings/time-zone", data={"time_zone": "Mars/Base"}, follow_redirects=False)
        assert "Couldn't find the time zone" in flash_of(r, "flash_error")
    finally:
        set_shown_zone(None)


def test_in_house_on_a_utc_computer_times_follow_the_company_zone(store, monkeypatch):
    monkeypatch.setattr(reports, "computer_on_utc", lambda: True)
    try:
        _lumen(store)
        reports.save_client(store, "lumen health", time_zone="America/Chicago")
        assert actions.local_time("2026-11-06T20:00:00+00:00", "%-I:%M %p %Z") == "2:00 PM CST"
    finally:
        set_shown_zone(None)


def test_a_date_only_stage_date_from_a_file_keeps_its_day_in_every_zone(store):
    be, ana, maya = _acme(store)
    reports.save_client(store, "acme", time_zone="Pacific/Honolulu")
    midnight = parse_when("2026-11-03")  # the day a file gave, read on this computer's clock
    with store._lock, store._conn:
        store._conn.execute(
            "INSERT INTO timeline(candidate_id, job_id, kind, body, at, created_at, source, data)"
            " VALUES (?, ?, 'stage', 'Client interview', ?, ?, 'ATS sync', ?)",
            (ana, be["id"], midnight, midnight, json.dumps({"by": "sync"})))
    rep = reports.report(store, "acme", since="2026-11-01", until="2026-11-05")
    line = next(x for x in reports.report_text(rep).splitlines() if "Ana Ruiz to Client interview" in x)
    assert line.startswith("- Tue Nov 3, 2026:")


# ------------------------------------------------------ 6. titles and the rest
def test_agency_funnel_says_the_jobs_own_stage_names(store):
    be, ana, maya = _acme(store)
    store.move_stage(maya, be["id"], "Placed", by="app")
    out = reports.report_text(reports.report(store, "acme"))
    assert "Moved into Submitted 2, Client interview 0, Offer 0, Placed 1" in out
    assert "Hired" not in out and "Interviewing" not in out


def test_a_closed_managers_report_gives_their_last_sent_copy(store):
    j, mei, jordan = _lumen(store)
    other = store.add_job("Nurse Lead", company="Lumen Health", stages=actions.stage_set("inhouse"),
                          hiring_manager="Dana Whitfield")
    bo = store.add_candidate(name="Aisha Bello")["id"]
    store.add_to_pipeline(bo, other["id"], "Applied")
    store.move_stage(bo, other["id"], "Hired", by="app")
    store.update_job(other["id"], status="closed")
    reports.mark_sent(store, "lumen health", manager="dana whitfield")
    res = call(build_server(store), "client_report", client="lumen", hiring_manager="dana")
    assert "Dana Whitfield has no update due" in res.content[0].text
    assert "Hired: Aisha Bello. Req closed: Nurse Lead." in res.content[1].text
    assert res.structuredContent["due"] is False


def test_a_long_update_gets_a_shorter_email_body_before_giving_up(store):
    be, ana, maya = _acme(store)
    reports.save_client(store, "acme", contacts="pat@acme.example")
    for i in range(30):
        cid = store.add_candidate(name=f"Person Number {i}")["id"]
        store.add_to_pipeline(cid, be["id"], "Sourced")
        store.move_stage(cid, be["id"], "Submitted", by="app")
    rep = reports.report(store, "acme")
    full = reports.report_text(rep)
    link = reports.email_link(rep, full)
    assert link["trimmed"] and not link["long"] and len(link["href"]) <= reports.MAIL_LIMIT
    assert "- and " in unquote(link["href"]) and "All time on these jobs" not in unquote(link["href"])
    assert "All time on these jobs" in full
    assert "shorter version to fit the link" in local_client(store).get("/clients/acme").text


def test_new_copy_has_no_long_dashes(store):
    be, ana, maya = _acme(store)
    _ai_move(store, maya, be["id"], "Placed")
    reports.save_client(store, "acme", update_day=_weekday(0))
    c = local_client(store)
    for path in ("/clients/acme", "/clients/due", "/settings"):
        body = c.get(path).text
        assert "—" not in body and "–" not in body
