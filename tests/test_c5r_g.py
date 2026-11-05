"""Cycle 5 re-review, fixer G: the launch check is bound to the port, a dead recorded port isn't trusted,
AI outcomes and AI-logged interviews are undoable and strikeable, and Undo all AI says what it did."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

from conftest import flash_of, local_client

from talent import cli, decide, paths, web
from talent.mcp_server import build_server
from test_mcp import call, structured


def _iso(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


class _Resp:
    def __init__(self, r):
        self.status_code, self._body = r.status_code, r.json()

    def json(self):
        return self._body


# ------------------------------------------------------------ 1. health proof bound to the port
def test_health_proof_is_bound_to_the_port_the_request_came_in_on(store):
    a = local_client(store, unlocked=False).get("/api/health", params={"nonce": "n1"}).json()["proof"]
    assert a == paths.health_proof(paths.app_key(), "n1", 8765)
    from fastapi.testclient import TestClient
    other = TestClient(web.create_app(store), base_url="http://127.0.0.1:9100")
    b = other.get("/api/health", params={"nonce": "n1"}).json()["proof"]
    assert b == paths.health_proof(paths.app_key(), "n1", 9100) and a != b
    # A Host header can't move it: the port is the socket's, not the header's.
    c = other.get("/api/health", params={"nonce": "n1"}, headers={"Host": "127.0.0.1:8765"}).json()["proof"]
    assert c == b


def test_a_relay_to_the_real_app_gets_no_launch_code(store, monkeypatch):
    real = local_client(store, unlocked=False)  # the real app, on 8765
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))

    def relay(url, params=None, timeout=None):  # whatever port was dialed, the answer comes from the real app
        return _Resp(real.get("/api/health", params=params))

    monkeypatch.setattr(cli.httpx, "get", relay)
    cli._write_port_file(8907)  # the squatter's port is the recorded one
    assert cli._probe(8907) == "other"
    assert cli._probe(8765) == "ours"  # the real app, asked on its own port
    # run_app asked for the squatter's port: no code goes there; the real app (on 8765) gets it.
    import uvicorn
    ran = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: ran.append(kw["port"]))
    monkeypatch.setattr(cli, "_port_free", lambda port: False)
    cli.run_app(port=8907)
    assert ran == []
    assert len(opened) == 1 and opened[0].startswith("http://127.0.0.1:8765/launch?code=")
    assert paths.port_file().read_text() == "8765"  # the record is fixed to where the app really is


# ------------------------------------------------------------ 2. a dead recorded port
def test_the_recorded_port_is_probed_first(monkeypatch):
    opened, ran = [], []
    import uvicorn
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: ran.append(kw["port"]))
    monkeypatch.setattr(cli, "_already_running", lambda port: port == 57183)
    cli._write_port_file(57183)
    cli.run_app(port=8765)
    assert ran == [] and opened[0].startswith("http://127.0.0.1:57183/launch?code=")


def test_a_dead_recorded_port_is_dropped_and_rewritten(monkeypatch):
    import uvicorn
    monkeypatch.setattr(cli, "_already_running", lambda port: False)
    cli._write_port_file(57183)
    assert cli.open_window() is False
    assert paths.port_file().read_text() == "57183"  # just written: that app may still be starting
    old = time.time() - cli.PORT_GRACE - 5
    os.utime(paths.port_file(), (old, old))
    assert cli.open_window() is False
    assert not paths.port_file().exists()  # a killed run's port isn't trusted any more
    # On start the port is recorded, and cleared again when the server stops.
    cli._write_port_file(57183)
    os.utime(paths.port_file(), (old, old))
    seen = []
    monkeypatch.setattr(cli, "_pick_port", lambda port: 9124)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.append(paths.port_file().read_text()))
    cli.run_app(port=8765, open_browser=False)
    assert seen == ["9124"] and not paths.port_file().exists()


# ------------------------------------------------------------ 3. AI outcomes and interviews undo
def _planted(store):
    """The AI app logs an interview that already happened, plants feedback, then records "Not passed"."""
    cid = store.add_candidate(name="Priya Raman")["id"]
    jid = store.add_job("Backend", "Acme")["id"]
    store.add_to_pipeline(cid, jid, "Interviewing")
    server = build_server(store)
    iv = structured(call(server, "log_activity", candidate_id=cid, kind="interview", summary="Onsite: system design",
                         when=_iso(-1), job_id=jid, interviewer="Sam"))["logged"]
    call(server, "add_interview_feedback", interview_id=iv["id"], feedback="Weak on basics", interviewer="Sam")
    call(server, "update_interview", interview_id=iv["id"], outcome="not passed", note="Not a fit")
    return cid, jid, iv["id"]


def test_ai_outcome_undoes_first_and_then_the_feedback_under_it(store):
    cid, jid, ivid = _planted(store)
    acts = {a["kind"]: a for a in store.ai_actions()}
    outcome, feedback, logged = acts["interview_outcome"], acts["interview_feedback"], acts["note_logged"]
    assert outcome["undoable"] and not outcome["record"]
    assert outcome["summary"] == "Recorded how an interview with Priya Raman went: Not passed"
    assert logged["summary"] == "Logged an interview with Priya Raman: Onsite: system design"
    for a in (feedback, logged):  # each waits for the newer one, and still counts for Undo all
        assert a["blocked"] == "your AI app changed it again later. Undo that first" and a["undo_pending"]
    c = local_client(store)
    assert "Can't undo: your AI app changed it again later. Undo that first." in c.get("/changes").text
    r = c.post(f"/changes/ai/{outcome['id']}/undo", follow_redirects=False)
    assert flash_of(r) == "Undid: Recorded how an interview with Priya Raman went: Not passed."
    iv = store.get_interview(ivid)
    assert iv["status"] == "scheduled" and "outcome_note" not in iv["data"]
    assert store.ai_action(feedback["id"])["undoable"]  # no longer locked by the outcome
    r = c.post(f"/changes/ai/{feedback['id']}/undo", follow_redirects=False)
    assert flash_of(r).startswith("Undid: Added interview feedback on Priya Raman from Sam")
    assert not store.get_interview(ivid)["data"].get("feedback")
    r = c.post(f"/changes/ai/{logged['id']}/undo", follow_redirects=False)
    assert "struck out, kept on the timeline" in flash_of(r)
    iv = store.get_interview(ivid)
    assert iv["data"]["struck_at"] and iv["status"] == "cancelled"


def test_undo_all_clears_the_whole_chain_and_the_interview_leaves_every_schedule(store):
    cid, jid, ivid = _planted(store)
    later = structured(call(build_server(store), "log_activity", candidate_id=cid, kind="interview",
                            summary="Final round", when=_iso(2), job_id=jid))["logged"]
    assert any(t["id"] == later["id"] for t in store.upcoming_interviews())
    c = local_client(store)
    r = c.post("/changes/undo-ai", data={"window": "1"}, follow_redirects=False)
    msg = flash_of(r)
    assert msg.startswith("Undid 4 AI changes"), msg
    assert "Left" not in msg, msg
    assert all(a["undone_at"] for a in store.ai_actions())
    assert not any(t["id"] in (ivid, later["id"]) for t in store.upcoming_interviews())
    assert decide._interviews(store, cid, jid) == []
    page = c.get(f"/candidates/{cid}").text
    assert page.count("Struck out. Reports, search and your AI app leave it out.") == 2
    assert f'id="iv-{later["id"]}"' not in page  # no reschedule or outcome form on a struck interview
    # Put back: on the schedule again, as it was.
    r = c.post(f"/candidates/{cid}/timeline/{later['id']}/unstrike", follow_redirects=False)
    assert r.status_code == 303
    back = store.get_interview(later["id"])
    assert back["status"] == "scheduled" and "struck_cancelled" not in back["data"]
    assert any(t["id"] == later["id"] for t in store.upcoming_interviews())


def test_ai_logged_interview_is_strikeable_and_the_recruiters_own_is_not(store):
    cid = store.add_candidate(name="Ben Okafor")["id"]
    server = build_server(store)
    ai_iv = structured(call(server, "log_activity", candidate_id=cid, kind="interview", summary="Phone screen",
                            when=_iso(3)))["logged"]
    own = store.log(cid, "interview", "Coffee chat", at=_iso(4))
    rows = {t["id"]: t for t in store.timeline(cid)}
    assert rows[ai_iv["id"]]["strikable"] and not rows[own["id"]]["strikable"]
    c = local_client(store)
    r = c.post(f"/candidates/{cid}/timeline/{ai_iv['id']}/strike", follow_redirects=False)
    assert r.status_code == 303 and store.get_interview(ai_iv["id"])["status"] == "cancelled"
    a = next(a for a in store.ai_actions() if a["kind"] == "note_logged")
    assert a["undone_at"]  # striking it on the timeline marks the AI change undone on Changes
    r = c.post(f"/candidates/{cid}/timeline/{own['id']}/strike", follow_redirects=False)
    assert "your AI app logged" in flash_of(r, "error") and store.get_interview(own["id"])["status"] == "scheduled"


def test_the_recruiters_later_change_keeps_an_ai_interview(store):
    cid = store.add_candidate(name="Ana Diaz")["id"]
    iv = structured(call(build_server(store), "log_activity", candidate_id=cid, kind="interview",
                         summary="Screen", when=_iso(-1)))["logged"]
    store.update_interview(iv["id"], outcome="passed")  # the recruiter recorded how it went
    a = next(a for a in store.ai_actions() if a["kind"] == "note_logged")
    assert not a["undoable"] and a["blocked"] == "that interview was changed again since"
    r = local_client(store).post("/changes/undo-ai", data={"window": "1"}, follow_redirects=False)
    assert store.get_interview(iv["id"])["status"] == "passed"
    assert "no AI changes" in flash_of(r) or "Left" in flash_of(r)


# ------------------------------------------------------------ 4. Undo all AI says what it did
def test_undo_all_says_what_it_undid_even_without_the_message_cookie(store):
    cid = store.add_candidate(name="Lee Chen")["id"]
    call(build_server(store), "log_activity", candidate_id=cid, kind="note", summary="Planted note")
    c = local_client(store)
    r = c.post("/changes/undo-ai", data={"window": "24"}, follow_redirects=False)
    assert r.headers["location"].startswith("/changes")
    assert flash_of(r).startswith("Undid 1 AI change: Logged a note on Lee Chen")
    # The flash shows once; the page also keeps the last Undo all for a day, for a browser that lost it.
    fresh = local_client(store)
    page = fresh.get("/changes").text
    assert "Last Undo all AI, " in page and "Undid 1 AI change: Logged a note on Lee Chen" in page
    assert "—" not in page and "–" not in page
    shown = c.get("/changes", cookies={k: v for k, v in r.cookies.items()}).text
    assert shown.count("Undid 1 AI change") == 1  # not twice while the flash itself is showing


def test_a_long_notice_in_another_script_still_fits_in_its_cookie():
    value = web.encode_flash("k" * 43, {"notice": "Undid 30 AI changes: " + "名前さん; " * 200})
    assert len(value) < 3100
    assert web.decode_flash(value, "k" * 43)["notice"].startswith("Undid 30 AI changes: ")
