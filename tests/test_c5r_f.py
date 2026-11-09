"""Cycle 5 re-review, fixer F: Cautious mode and the AI caps cover every AI write (client contacts, Mark as sent,
interview changes and feedback, logged interviews, notes sent with an add, stage edits), "Contacts changed by your
AI app" on the client and send-out pages, one shared "reviewed" state, and the queue bugs (duplicate notes, a note
kept twice, a database error on a deleted job, struck notes counting toward the cap)."""
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from talent import actions, reports, review, store as store_mod
from talent.mcp_server import build_server
from talent.store import AI_NOTE_CAP

from conftest import flash_of, local_client



@pytest.fixture(autouse=True)
def _trusted_apps_may_hire(store):
    """Cycle 10 wave 2: Hired and Close job wait for review by default. These tests are about what happens after an
    AI app hires or closes, so they turn on the Settings switch that lets a trusted app do it."""
    store.set_settings(ai_trusted_hired="1")

def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def data(res) -> dict:
    return res.structuredContent if isinstance(res, CallToolResult) else res[1].get("result", res[1])


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()


def _today() -> str:
    return reports.UPDATE_DAYS[datetime.now().astimezone().weekday()]


def _cautious(store):
    review.set_cautious(store, True)
    return local_client(store)


def _acme(store, contacts="Lee Moss <lee@acme.com>"):
    job = store.add_job("Backend Engineer", company="Acme", stages=actions.stage_set("agency"))
    cid = store.add_candidate(name="Maya Chen")["id"]
    store.add_to_pipeline(cid, job["id"], "Submitted")
    reports.save_client(store, "acme", contacts=contacts, update_day=_today())
    return job["id"], cid


def _past_interview(store, cid, jid, who="Priya"):
    return store.log(cid, "interview", "Onsite: system design", at=_ago(3), job_id=jid, interviewer=who)["id"]


def _future_interview(store, cid, jid):
    return store.log(cid, "interview", "Onsite: coding", at=_ago(-48), job_id=jid)["id"]


def _no_dash(body: str) -> None:
    assert "—" not in body and " – " not in body


# ------------------------------------------------------------ 1. every AI write path waits in Cautious mode
def test_cautious_update_client_contacts_wait_and_keep_applies_once(store):
    server = build_server(store)
    c = _cautious(store)
    _acme(store)
    res = call(server, "update_client", client="acme", contacts="attacker@evil.example")
    t = text(res)
    assert "Waiting for your review: Change \"Acme\"'s contacts" in t and "Nothing changed" in t
    assert reports.client(store, "acme")["contacts"] == "Lee Moss <lee@acme.com>"
    assert "attacker@evil.example" not in c.get("/clients/due").text
    page = c.get("/review").text
    assert "Client change" in page and "was Lee Moss &lt;lee@acme.com&gt;, would be attacker@evil.example" in page
    _no_dash(page)
    rid = data(res)["pending_review"]["id"]
    r = c.post(f"/review/{rid}/keep", follow_redirects=False)
    assert flash_of(r).startswith("Kept: Change")
    assert reports.client(store, "acme")["contacts"] == "attacker@evil.example"
    # Kept on Review is reviewed: no "Contacts changed by your AI app" flag after it.
    assert "Contacts changed by your AI app" not in c.get("/clients/acme").text
    # And it is still the AI app's change, with Undo on Changes.
    a = next(a for a in store.ai_actions() if a["kind"] == "client_edit")
    store.undo_ai_action(a["id"])
    assert reports.client(store, "acme")["contacts"] == "Lee Moss <lee@acme.com>"


def test_cautious_update_client_fills_an_empty_day_but_holds_a_replaced_zone(store):
    server = build_server(store)
    _cautious(store)
    store.add_job("Backend Engineer", company="Globex")
    reports.save_client(store, "globex", time_zone="ET")
    res = call(server, "update_client", client="globex", update_day="Friday", time_zone="PT")
    c = reports.client(store, "globex")
    assert c["update_day"] == "Friday" and c["time_zone"] == "America/New_York"
    assert "Held for your review: Change \"Globex\"'s time zone" in text(res)
    with pytest.raises(ToolError, match="day of the week"):
        call(server, "update_client", client="globex", update_day="Someday")


def test_cautious_mark_update_sent_waits_and_a_stale_one_is_refused(store):
    server = build_server(store)
    c = _cautious(store)
    jid, cid = _acme(store)
    res = call(server, "mark_update_sent", client="acme")
    assert text(res).startswith("Waiting for your review: Mark the update to \"Acme\" as sent")
    assert reports.client(store, "acme")["last_update_sent_at"] is None
    rid = data(res)["pending_review"]["id"]
    review.keep(store, rid)
    assert reports.client(store, "acme")["last_update_sent_at"]
    # Queued, then the recruiter marked it sent themselves: keeping the old one doesn't mark it again.
    rid = data(call(server, "mark_update_sent", client="acme"))["pending_review"]["id"]
    store.move_stage(cid, jid, "Client interview")  # something new, so the recruiter's own mark is a real send
    reports.mark_sent(store, "acme")
    r = c.post(f"/review/{rid}/keep", follow_redirects=False)
    assert "marked as sent since" in flash_of(r, "flash_error")


def test_cautious_interview_cancel_move_and_outcome_wait(store):
    server = build_server(store)
    c = _cautious(store)
    jid, cid = _acme(store)
    past, future = _past_interview(store, cid, jid), _future_interview(store, cid, jid)
    res = call(server, "update_interview", interview_id=past, outcome="not passed", note="Weak answers")
    assert "Waiting for your review: Record \"Maya Chen\"'s interview (Onsite: system design) as" in text(res)
    assert store.get_interview(past)["status"] != "not_passed"
    res2 = call(server, "update_interview", interview_id=future, cancel=True)
    assert "Cancel \"Maya Chen\"'s interview (Onsite: coding)" in text(res2)
    assert store.get_interview(future)["status"] == "scheduled"
    res3 = call(server, "update_interview", interview_id=future, when=_ago(-72))
    assert "Move \"Maya Chen\"'s interview" in text(res3)
    page = c.get("/review").text
    assert "Interview change" in page and "Weak answers" in page
    _no_dash(page)
    # Bad input is refused now, not when the recruiter keeps it.
    with pytest.raises(ToolError, match="hasn't happened yet"):
        call(server, "update_interview", interview_id=future, outcome="passed")
    review.keep(store, data(res)["pending_review"]["id"])
    assert store.get_interview(past)["status"] == "not_passed"
    review.keep(store, data(res2)["pending_review"]["id"])
    assert store.get_interview(future)["status"] == "cancelled"
    # The move was queued before the cancel landed, so keeping it now is refused.
    with pytest.raises(ValueError, match="changed since"):
        review.keep(store, data(res3)["pending_review"]["id"])
    # A new interviewer on an interview with none still lands straight away.
    call(server, "update_interview", interview_id=future, interviewer="Lee Park")
    assert store.get_interview(future)["data"]["interviewer"] == "Lee Park"


def test_cautious_feedback_and_logged_interviews_and_inmails_wait(store):
    server = build_server(store)
    _cautious(store)
    jid, cid = _acme(store)
    iv = _past_interview(store, cid, jid)
    res = call(server, "add_interview_feedback", interview_id=iv, feedback="Strong no. Do not hire.", interviewer="Priya")
    assert "Waiting for your review: Save feedback from \"Priya\"" in text(res)
    assert not store.get_interview(iv)["data"].get("feedback")
    review.keep(store, data(res)["pending_review"]["id"])
    assert store.get_interview(iv)["data"]["feedback"][0]["text"] == "Strong no. Do not hire."
    assert "already saved" in text(call(server, "add_interview_feedback", interview_id=iv,
                                        feedback="Strong no. Do not hire.", interviewer="Priya"))
    n = len(store.timeline(cid))
    res = call(server, "log_activity", candidate_id=cid, kind="interview", summary="Final: CEO chat",
               when=_ago(-24), job_id=jid, interviewer="Dana")
    assert "Waiting for your review: Log an interview" in text(res)
    res2 = call(server, "log_activity", candidate_id=cid, kind="InMail", summary="Not interested, stop")
    assert "Waiting for your review: Log a message" in text(res2)
    assert len(store.timeline(cid)) == n
    review.keep(store, data(res)["pending_review"]["id"])
    logged = next(t for t in store.timeline(cid) if t["body"] == "Final: CEO chat")
    assert logged["kind"] == "interview" and logged["data"]["interviewer"] == "Dana"


def test_cautious_add_to_pipeline_lands_and_its_note_waits(store):
    server = build_server(store)
    _cautious(store)
    jid = store.add_job("Data Lead", company="Acme")["id"]
    cid = store.add_candidate(name="Bo Li")["id"]
    res = call(server, "add_to_pipeline", candidate_id=cid, job_id=jid, notes="Asked for 400k, flag to client")
    assert "Added \"Bo Li\" to Data Lead (Acme)" in text(res) and "Held for your review: Log a note" in text(res)
    assert store.candidate_pipelines(cid)[0]["job_id"] == jid
    assert not [t for t in store.timeline(cid) if t["kind"] == "note"]
    assert store.candidate_pipelines(cid)[0].get("notes") in (None, "")


def test_cautious_edit_job_stages_waits_is_checked_now_and_stale_is_refused(store):
    server = build_server(store)
    c = _cautious(store)
    jid = store.add_job("Backend", company="Acme")["id"]
    res = call(server, "edit_job_stages", job_id=jid, rename={"Screening": "Phone screen"})
    assert "Waiting for your review: Change the stages of Backend (Acme)" in text(res)
    assert "Screening" in [s["name"] for s in store.job_stages(jid)]
    assert "Stage change" in c.get("/review").text
    with pytest.raises(ToolError, match="No change made"):
        call(server, "edit_job_stages", job_id=jid, rename={"Hired": "Sourced again"})
    assert [s["name"] for s in store.job_stages(jid)].count("Hired") == 1  # the check took nothing with it
    review.keep(store, data(res)["pending_review"]["id"])
    assert "Phone screen" in [s["name"] for s in store.job_stages(jid)]
    assert any(a["kind"] == "stages_edit" for a in store.ai_actions())
    rid = data(call(server, "edit_job_stages", job_id=jid, add=["Take-home"]))["pending_review"]["id"]
    store.rename_stage(jid, "Offer", "Offer out")
    with pytest.raises(ValueError, match="stages were changed since"):
        review.keep(store, rid)


# ------------------------------------------------------------ 1b. the caps cover them too
def test_overwrite_cap_covers_clients_sends_interviews_and_stages(store, monkeypatch):
    monkeypatch.setattr(store_mod, "AI_OVERWRITE_CAP", 1)
    server = build_server(store)
    jid, cid = _acme(store)
    iv = _past_interview(store, cid, jid)
    call(server, "update_client", client="acme", contacts="lee@acme.com")  # one replaced value: the cap is full
    for tool, args in (("update_client", {"client": "acme", "contacts": "x@evil.example"}),
                       ("mark_update_sent", {"client": "acme"}),
                       ("update_interview", {"interview_id": iv, "outcome": "not passed"}),
                       ("edit_job_stages", {"job_id": jid, "rename": {"Screened": "Phoned"}})):
        with pytest.raises(ToolError, match="already replaced or cleared"):
            call(server, tool, **args)
    assert reports.client(store, "acme")["contacts"] == "lee@acme.com"
    assert store.get_interview(iv)["status"] != "not_passed"


def test_note_cap_counts_feedback_and_move_and_add_notes(store):
    server = build_server(store)
    jid, cid = _acme(store)
    other = store.add_job("Data Lead", company="Acme")["id"]
    iv = _past_interview(store, cid, jid)
    for i in range(AI_NOTE_CAP - 3):
        call(server, "add_interview_feedback", interview_id=iv, feedback=f"Point {i}", interviewer=f"P{i}")
    call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Client interview", notes="Moved on")
    call(server, "add_to_pipeline", candidate_id=cid, job_id=other, notes="Also fits here")
    call(server, "log_activity", candidate_id=cid, kind="note", summary="One more")
    assert store.ai_notes_this_hour(cid) == AI_NOTE_CAP
    with pytest.raises(ToolError, match=f"already logged {AI_NOTE_CAP} entries"):
        call(server, "add_interview_feedback", interview_id=iv, feedback="Too many", interviewer="Q")
    with pytest.raises(ToolError, match="Nothing moved"):
        call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Offer", notes="Moving")
    app = next(p for p in store.candidate_pipelines(cid) if p["job_id"] == jid)
    assert app["stage"] == "Client interview"
    # A move without a note still works.
    call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Offer")


def test_waiting_notes_count_toward_the_note_cap(store):
    server = build_server(store)
    _cautious(store)
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    for i in range(AI_NOTE_CAP):
        call(server, "log_activity", candidate_id=cid, kind="note", summary=f"Note {i}")
    assert review.count(store) == AI_NOTE_CAP
    with pytest.raises(ToolError, match="Nothing logged"):
        call(server, "log_activity", candidate_id=cid, kind="note", summary="Note 11")


def test_struck_notes_dont_count_toward_the_note_cap(store):
    server = build_server(store)
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    for i in range(AI_NOTE_CAP):
        call(server, "log_activity", candidate_id=cid, kind="note", summary=f"Planted {i}")
    with pytest.raises(ToolError):
        call(server, "log_activity", candidate_id=cid, kind="interview", summary="Real onsite", when=_ago(-24))
    for t in store.timeline(cid):
        if t["strikable"]:
            store.strike_entry(t["id"])
    assert store.ai_notes_this_hour(cid) == 0
    call(server, "log_activity", candidate_id=cid, kind="interview", summary="Real onsite", when=_ago(-24))


# ------------------------------------------------------------ 2. contacts changed by the AI app
def test_ai_contacts_change_is_flagged_by_email_until_kept(store):
    server = build_server(store)
    _acme(store)
    call(server, "update_client", client="acme", contacts="attacker@evil.example")
    c = local_client(store)
    for path in ("/clients/acme", "/clients/due"):
        body = c.get(path).text
        assert "Contacts changed by your AI app" in body and "attacker@evil.example" in body
        assert "(was Lee Moss &lt;lee@acme.com&gt;)" in body
        flag = body.split("Contacts changed by your AI app")[1]
        email = body.split("Contacts changed by your AI app")[1].split("Email")[0]
        assert "/confirm" in email and "/undo" in email and "Email" in flag  # Keep and Undo, next to Email
        _no_dash(body)
    cx = review.contacts_change(store, "acme")
    r = c.post(f"/changes/ai/{cx['id']}/confirm", headers={"referer": "http://127.0.0.1:8765/clients/acme"},
               follow_redirects=False)
    assert r.headers["location"].startswith("/clients/acme") and flash_of(r).startswith("Kept:")
    assert "Contacts changed by your AI app" not in c.get("/clients/due").text
    assert reports.client(store, "acme")["contacts"] == "attacker@evil.example"


def test_ai_contacts_change_undo_puts_them_back_and_a_recruiter_edit_clears_it(store):
    server = build_server(store)
    _acme(store)
    call(server, "update_client", client="acme", contacts="attacker@evil.example")
    c = local_client(store)
    cx = review.contacts_change(store, "acme")
    c.post(f"/changes/ai/{cx['id']}/undo", headers={"referer": "http://127.0.0.1:8765/clients/due"})
    assert reports.client(store, "acme")["contacts"] == "Lee Moss <lee@acme.com>"
    assert review.contacts_change(store, "acme") is None
    call(server, "update_client", client="acme", contacts="new@acme.com")
    assert review.contacts_change(store, "acme")
    reports.save_client(store, "acme", contacts="mine@acme.com")  # the recruiter set them: nothing to flag
    assert review.contacts_change(store, "acme") is None


# ------------------------------------------------------------ 3. one reviewed state
def _ai_hire(server, store):
    jid, cid = _acme(store)
    call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Placed")
    return jid, cid


def _today_hire(store):
    return [i for i in actions.action_items(store)["items"] if i["key"].startswith("aihire:")]


def _due_flagged(store) -> bool:
    return bool(reports.report(store, "acme")["ai_moves"])


def test_yes_on_today_clears_the_send_out_warning(store):
    server = build_server(store)
    _ai_hire(server, store)
    assert _due_flagged(store) and _today_hire(store)
    c = local_client(store)
    c.post(f"/changes/ai/{_today_hire(store)[0]['confirm_id']}/confirm")
    assert not _due_flagged(store) and not _today_hire(store)
    assert "haven't reviewed" not in c.get("/clients/due").text


def test_mark_reviewed_on_a_client_page_clears_todays_right(store):
    server = build_server(store)
    _ai_hire(server, store)
    aid = reports.report(store, "acme")["ai_moves"][0]["action_id"]
    local_client(store).post("/clients/acme/ai-reviewed", data={"action_id": str(aid)})
    assert not _today_hire(store) and not _due_flagged(store)


def test_keep_on_review_clears_both(store):
    server = build_server(store)
    _cautious(store)
    jid, cid = _ai_hire(server, store)
    assert not _due_flagged(store)  # it waits, so it isn't in the update yet
    review.keep(store, review.pending(store)[0]["id"])
    assert next(p for p in store.candidate_pipelines(cid) if p["job_id"] == jid)["stage"] == "Placed"
    assert not _due_flagged(store) and not _today_hire(store)


# ------------------------------------------------------------ 4. queue bugs
def test_the_same_note_sent_seconds_apart_waits_once_and_is_logged_once(store, monkeypatch):
    server = build_server(store)
    _cautious(store)
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    a = data(call(server, "log_activity", candidate_id=cid, kind="note", summary="Do not contact"))
    stamps = iter(["2026-11-05T10:00:05+00:00", "2026-11-05T10:00:09+00:00"])
    monkeypatch.setattr(review, "now_iso", lambda: next(stamps))
    b = data(call(server, "log_activity", candidate_id=cid, kind="note", summary="Do not contact"))
    assert a["pending_review"]["id"] == b["pending_review"]["id"] and review.count(store) == 1
    monkeypatch.undo()
    review.keep(store, a["pending_review"]["id"])
    c = data(call(server, "log_activity", candidate_id=cid, kind="note", summary="Do not contact"))
    assert "already logged" in review.keep(store, c["pending_review"]["id"])
    assert len([t for t in store.timeline(cid) if t["kind"] == "note"]) == 1
    # A note with its own time is its own entry.
    d = data(call(server, "log_activity", candidate_id=cid, kind="note", summary="Do not contact", when=_ago(30)))
    assert d["pending_review"]["id"] != c["pending_review"]["id"]


def test_move_note_on_a_deleted_job_says_so_plainly(store):
    server = build_server(store)
    _cautious(store)
    jid = store.add_job("Gone")["id"]
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    store.add_to_pipeline(cid, jid)
    store.delete_job(jid)
    with pytest.raises(ToolError) as exc:
        call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Contacted", notes="Called")
    assert "FOREIGN KEY" not in str(exc.value) and f"No job with id {jid}" in str(exc.value)
    assert review.count(store) == 0
    # In play but not in this job's pipeline: no note is left waiting either.
    other = store.add_job("Other")["id"]
    with pytest.raises(ToolError):
        call(server, "move_stage", candidate_id=cid, job_id=other, stage="Contacted", notes="Called")
    assert review.count(store) == 0


@pytest.mark.parametrize("path", ["/review", "/clients/acme", "/clients/due", "/settings"])
def test_new_copy_has_no_dashes(store, path):
    server = build_server(store)
    _acme(store)
    call(server, "update_client", client="acme", contacts="x@evil.example")
    _cautious(store)
    call(server, "mark_update_sent", client="acme")
    _no_dash(local_client(store).get(path).text)
