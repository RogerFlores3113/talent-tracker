"""Cycle 5, fixer A: a launcher that checks the server proves itself, the AI overwrite and note caps, struck-out AI
notes, AI hires and job closes confirmed on Today, the AI review queue (Cautious mode), and Undo all AI with a
backup and a chosen window."""
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from talent import actions, backups, cli, paths, review
from talent.mcp_server import build_server
from talent.store import AI_NOTE_CAP, AI_OVERWRITE_ALERT, AI_OVERWRITE_CAP

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


def _ai_items(store) -> list[dict]:
    return [i for i in actions.action_items(store)["items"] if i["kind"] == "ai_change"]


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()


# ------------------------------------------------------------ 1. health proof
class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


def test_health_answers_a_proof_only_this_install_can_make(store):
    c = local_client(store, unlocked=False)
    assert c.get("/api/health").json() == {"ok": True, "version": c.get("/api/health").json()["version"]}
    body = c.get("/api/health", params={"nonce": "abc123"}).json()
    assert body["proof"] == paths.health_proof(paths.app_key(), "abc123", 8765)  # c5 G: bound to the port too
    assert body["proof"] != paths.health_proof("another install's key" * 2, "abc123", 8765)
    assert "candidates" not in body  # still says nothing about the data while locked


def test_launcher_sends_no_code_to_an_impostor(monkeypatch):
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    # Something on the port answers like the app (a version), but can't prove it.
    monkeypatch.setattr(cli.httpx, "get", lambda url, params=None, timeout=None: _Resp({"ok": True, "version": "9"}))
    assert cli._probe(8765) == "other"
    assert cli.open_window() is False and opened == []
    # A copied proof for some other nonce doesn't work either.
    monkeypatch.setattr(cli.httpx, "get", lambda url, params=None, timeout=None: _Resp(
        {"ok": True, "version": "9", "proof": paths.health_proof(paths.app_key(), "old-nonce", 8765)}))
    assert cli._probe(8765) == "other"
    # The real app answers the HMAC of the nonce it was sent.
    monkeypatch.setattr(cli.httpx, "get", lambda url, params=None, timeout=None: _Resp(
        {"ok": True, "version": "9", "proof": paths.health_proof(paths.app_key(), params["nonce"], 8765)}))
    assert cli._probe(8765) == "ours"
    assert cli.open_window() is True and len(opened) == 1 and "/launch?code=" in opened[0]


def test_serve_says_why_when_the_port_is_taken(monkeypatch, caplog):
    import uvicorn

    ran = []
    monkeypatch.setattr(cli, "_already_running", lambda port: False)
    monkeypatch.setattr(cli, "_pick_port", lambda port: 9123)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: ran.append(kw["port"]))
    with caplog.at_level("INFO"):
        cli.run_app(port=8765, open_browser=False)
    assert ran == [9123]
    assert "Port 8765 is in use by another program that isn't this Talent Search" in caplog.text
    assert "Starting on port 9123 instead" in caplog.text
    caplog.clear()
    monkeypatch.setattr(cli, "_already_running", lambda port: True)
    with caplog.at_level("INFO"):
        cli.run_app(port=8765, open_browser=False)
    assert "already running at http://127.0.0.1:8765/" in caplog.text and ran == [9123]


# ------------------------------------------------------------ 2. caps
def test_overwrite_cap_across_everyone_and_a_today_alert(store):
    server = build_server(store)
    ids = [store.add_candidate(name=f"Person {i}", current_title="Engineer")["id"] for i in range(AI_OVERWRITE_CAP + 1)]
    for cid in ids[:AI_OVERWRITE_ALERT - 1]:
        call(server, "update_candidate", candidate_id=cid, current_title="Staff Engineer")
    assert not [i for i in _ai_items(store) if "Review or undo all" in i["title"]]
    for cid in ids[AI_OVERWRITE_ALERT - 1:AI_OVERWRITE_CAP]:
        call(server, "update_candidate", candidate_id=cid, current_title="Staff Engineer")
    alert = next(i for i in _ai_items(store) if "Review or undo all" in i["title"])
    assert alert["title"] == (f"Your AI app changed {AI_OVERWRITE_CAP} saved values on {AI_OVERWRITE_CAP} people in "
                              "the last hour. Review or undo all.")
    assert alert["detail"] == "It can't change more until the hour has passed."
    with pytest.raises(ToolError, match=f"already replaced or cleared {AI_OVERWRITE_CAP} saved values this hour"):
        call(server, "update_candidate", candidate_id=ids[-1], current_title="Staff Engineer")
    assert store.get_candidate(ids[-1])["current_title"] == "Engineer"
    # Filling an empty field still works, and so does a job close... no: a close replaces a saved status.
    call(server, "update_candidate", candidate_id=ids[-1], location="Austin")
    jid = store.add_job("Backend")["id"]
    with pytest.raises(ToolError, match="already replaced or cleared"):
        call(server, "update_job", job_id=jid, status="closed")
    assert store.get_job(jid)["status"] == "open"
    # A merge through save_candidate keeps the saved title and says why.
    store.update_candidate(ids[-1], email="p@x.com")
    res = call(server, "save_candidate", name=f"Person {AI_OVERWRITE_CAP}", email="p@x.com", current_title="CTO")
    assert "already replaced or cleared" in text(res) and data(res)["budget_kept"] == ["current_title"]


def test_job_closes_count_toward_the_cap(store):
    server = build_server(store)
    jid = store.add_job("Backend", company="Acme")["id"]
    call(server, "update_job", job_id=jid, status="closed", location="Remote")  # location was empty: a fill
    got = store.ai_overwrites_this_hour()
    assert got["count"] == 1 and got["jobs"] == 1


def test_log_activity_is_capped_per_person_per_hour(store):
    server = build_server(store)
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    other = store.add_candidate(name="Bo Li")["id"]
    for i in range(AI_NOTE_CAP):
        call(server, "log_activity", candidate_id=cid, kind="note", summary=f"Note {i}")
    with pytest.raises(ToolError, match=f'already logged {AI_NOTE_CAP} entries on "Ana Ruiz" this hour'):
        call(server, "log_activity", candidate_id=cid, kind="note", summary="Do not contact")
    call(server, "log_activity", candidate_id=other, kind="note", summary="Fine")  # per person
    assert store.ai_notes_this_hour(cid) == AI_NOTE_CAP


# ------------------------------------------------------------ 3. struck notes
def test_strike_an_ai_email_and_last_contacted_comes_back(store):
    server = build_server(store)
    cid = store.add_candidate(name="Maya Okafor")["id"]
    store.log(cid, "call", "Intro call", at=_ago(48), source="App")
    before = store.get_candidate(cid)["last_contacted_at"]
    call(server, "log_activity", candidate_id=cid, kind="email",
         summary="Accepted another offer, do not contact again", when=_ago(1))
    assert store.get_candidate(cid)["last_contacted_at"] > before
    entry = next(t for t in store.timeline(cid) if t["kind"] == "email")
    assert entry["strikable"]
    c = local_client(store)
    page = c.get(f"/candidates/{cid}").text
    assert f"/timeline/{entry['id']}/strike" in page and "Strike out" in page
    c.post(f"/candidates/{cid}/timeline/{entry['id']}/strike")
    t = next(t for t in store.timeline(cid) if t["id"] == entry["id"])
    assert t["struck"] and not t["strikable"]  # kept, marked struck
    assert store.get_candidate(cid)["last_contacted_at"] == before
    assert "do not contact" not in text(call(server, "get_candidate", candidate_id=cid))
    assert all(h.candidate["id"] != cid for h in store.search_candidates("contact again").hits)
    page = c.get(f"/candidates/{cid}").text
    assert "Struck out." in page and " struck" in page and "Put back" in page
    # A note the recruiter typed can't be struck (only the AI's).
    mine = next(t for t in store.timeline(cid) if t["kind"] == "call")
    assert not mine["strikable"]
    r = c.post(f"/candidates/{cid}/timeline/{mine['id']}/strike", follow_redirects=False)
    assert r.status_code == 303 and "your AI app logged" in (flash_of(r, "error") + flash_of(r, "flash_error"))
    # Put back.
    c.post(f"/candidates/{cid}/timeline/{entry['id']}/unstrike")
    assert not next(t for t in store.timeline(cid) if t["id"] == entry["id"])["struck"]
    assert store.get_candidate(cid)["last_contacted_at"] > before


def test_undo_all_ai_strikes_ai_notes_and_takes_a_backup(store):
    server = build_server(store)
    cid = store.add_candidate(name="Lee Chen")["id"]
    call(server, "log_activity", candidate_id=cid, kind="note", summary="Not a fit, do not contact")
    c = local_client(store)
    page = c.get("/changes?who=ai").text
    assert "Logged a note on Lee Chen: Not a fit, do not contact" in page
    r = c.post("/changes/undo-ai", data={"window": "24"}, follow_redirects=False)
    msg = flash_of(r)
    assert msg.startswith("Undid 1 AI change: Logged a note on Lee Chen") and "struck out" in msg
    assert "A backup from just before is under Automatic backups in Settings." in msg
    assert any(b["why"] == "Before undoing AI changes" for b in backups.list_backups())
    assert next(t for t in store.timeline(cid) if t["kind"] == "note")["struck"]
    assert "do not contact" not in text(call(server, "get_candidate", candidate_id=cid))


def test_undo_all_window_is_the_users_pick(store):
    server = build_server(store)
    old, new = (store.add_candidate(name=n, current_title="Engineer")["id"] for n in ("Old Change", "New Change"))
    call(server, "update_candidate", candidate_id=old, current_title="CTO")
    call(server, "update_candidate", candidate_id=new, current_title="CTO")
    first = min(a["id"] for a in store.ai_actions())
    with store._write():
        store._conn.execute("UPDATE ai_actions SET at = ? WHERE id = ?", (_ago(3), first))
    c = local_client(store)
    page = c.get("/changes").text
    assert 'name="window"' in page and '<option value="24" selected>24 hours</option>' in page
    msg = flash_of(c.post("/changes/undo-ai", data={"window": "1"}, follow_redirects=False))
    assert msg.startswith("Undid 1 AI change")
    assert store.get_candidate(new)["current_title"] == "Engineer"
    assert store.get_candidate(old)["current_title"] == "CTO"  # 3 hours ago: outside the last hour


def test_undoing_an_ai_move_strikes_its_move_note(store):
    server = build_server(store)
    jid = store.add_job("Backend")["id"]
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    store.add_to_pipeline(cid, jid, "Screening")
    call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Rejected", notes="Not a fit")
    assert store.pipeline(jid)[0]["notes"] == "Not a fit"
    move = next(a for a in store.ai_actions() if a["kind"] == "stage_move")
    store.undo_ai_action(move["id"])
    note = next(t for t in store.timeline(cid) if t["kind"] == "note")
    assert note["struck"] and store.pipeline(jid)[0]["stage"] == "Screening"
    assert not store.pipeline(jid)[0]["notes"]


# ------------------------------------------------------------ 4. confirm hires and closes
def test_ai_hire_and_close_ask_right_with_yes_or_undo(store):
    server = build_server(store)
    job = store.add_job("Staff Backend Engineer", company="Acme")
    cid = store.add_candidate(name="Aisha Bello")["id"]
    store.add_to_pipeline(cid, job["id"], "Interviewing")
    res = call(server, "move_stage", candidate_id=cid, job_id=job["id"], stage="Hired")
    assert "asked on Today to confirm this hire" in text(res)
    item = next(i for i in _ai_items(store) if "Aisha Bello" in i["title"])
    assert item["title"] == "Your AI app marked Aisha Bello hired on Staff Backend Engineer (Acme). Right?"
    c = local_client(store)
    page = c.get("/").text
    assert f'/changes/ai/{item["confirm_id"]}/confirm' in page and f'/changes/ai/{item["confirm_id"]}/undo' in page
    r = c.post(f"/changes/ai/{item['confirm_id']}/confirm", headers={"referer": "http://127.0.0.1:8765/"},
               follow_redirects=False)
    assert flash_of(r).startswith("Kept: Moved Aisha Bello from Interviewing to Hired")
    assert not [i for i in _ai_items(store) if "Aisha Bello" in i["title"]]
    assert store.pipeline(job["id"])[0]["stage"] == "Hired"
    # A close asks too; Undo from Today reopens it.
    other = store.add_job("Data Lead")["id"]
    call(server, "update_job", job_id=other, status="closed")
    close = next(i for i in _ai_items(store) if i["title"] == "Your AI app closed Data Lead.")
    assert close["confirm_id"] and close["detail"].startswith("Right?")
    c.post(f"/changes/ai/{close['confirm_id']}/undo")
    assert store.get_job(other)["status"] == "open"


# ------------------------------------------------------------ 5. review queue
def _cautious(store):
    c = local_client(store)
    r = c.post("/settings/cautious", data={"ai_cautious": "1"}, follow_redirects=False)
    assert "Cautious mode is on" in flash_of(r)
    assert review.cautious(store)
    return c


def test_cautious_mode_is_off_by_default_and_a_settings_toggle(store):
    assert not review.cautious(store)
    c = local_client(store)
    page = c.get("/settings").text
    assert 'id="cautious"' in page and 'name="ai_cautious"' in page and "Off by default." in page
    _cautious(store)
    assert "checked" in c.get("/settings").text.split('name="ai_cautious"')[1][:40]
    c.post("/settings/cautious", data={})
    assert not review.cautious(store)


def test_cautious_overwrite_waits_and_a_blank_fill_applies(store):
    server = build_server(store)
    c = _cautious(store)
    cid = store.add_candidate(name="Ana Ruiz", current_title="Engineer")["id"]
    res = call(server, "update_candidate", candidate_id=cid, current_title="CTO", location="Austin")
    t = text(res)
    assert "Updated" in t and "Held for the recruiter's review: Change \"Ana Ruiz\"'s title" in t
    assert "The rest of this call went through" in t and data(res)["pending_review"]["id"]
    cand = store.get_candidate(cid)
    assert cand["current_title"] == "Engineer" and cand["location"] == "Austin"
    # Read back: the AI sees it as pending.
    got = text(call(server, "get_candidate", candidate_id=cid))
    assert "Waiting for the recruiter's review (not applied yet" in got and "title to 'CTO'" in got
    # Today and Review.
    item = next(i for i in _ai_items(store) if i["href"] == "/review")
    assert item["title"] == "1 change from your AI app waits for your review."
    page = c.get("/review").text
    assert "Change &#34;Ana Ruiz&#34;&#39;s title" in page
    assert "was Engineer, would be CTO" in page and "Keep all" in page
    assert "—" not in page and " – " not in page
    rid = data(res)["pending_review"]["id"]
    r = c.post(f"/review/{rid}/keep", follow_redirects=False)
    assert flash_of(r).startswith("Kept: Change")
    assert store.get_candidate(cid)["current_title"] == "CTO"
    # Exactly once: a second Keep (or a Pass after it) is refused and changes nothing.
    assert "already kept or passed" in flash_of(c.post(f"/review/{rid}/keep", follow_redirects=False), "flash_error")
    assert "already kept or passed" in flash_of(c.post(f"/review/{rid}/pass", follow_redirects=False), "flash_error")
    assert len([t for t in store.timeline(cid) if t["kind"] == "change" and "current_title" in t["data"]["new"]]) == 1
    changes = c.get("/changes").text
    assert "Kept on Review: Change" in changes
    # The kept change is the AI's, so it can still be undone on Changes.
    a = next(a for a in store.ai_actions() if a["kind"] == "profile")
    store.undo_ai_action(a["id"])
    assert store.get_candidate(cid)["current_title"] == "Engineer"
    assert not [i for i in _ai_items(store) if i["href"] == "/review"]


def test_cautious_pass_drops_it_and_new_people_still_save(store):
    server = build_server(store)
    c = _cautious(store)
    res = call(server, "save_candidate", name="Bo Li", email="bo@x.com", current_title="Engineer")
    assert data(res)["created"]  # a new person applies straight away
    res = call(server, "save_candidate", name="Bo Li", email="bo@x.com", current_title="VP Engineering",
               location="Denver")
    assert "Waiting for the recruiter's review" in text(res)
    bo = store.find_by_name("Bo Li")[0]
    assert bo["current_title"] == "Engineer" and bo["location"] == "Denver"
    rid = data(res)["pending_review"]["id"]
    r = c.post(f"/review/{rid}/pass", follow_redirects=False)
    assert flash_of(r).startswith("Passed: Change") and "Nothing was changed" in flash_of(r)
    assert store.get_candidate(bo["id"])["current_title"] == "Engineer"
    assert "Passed on Review: Change" in c.get("/changes?who=app").text


def test_cautious_hire_move_close_and_note_wait_then_keep_all(store):
    server = build_server(store)
    c = _cautious(store)
    jid = store.add_job("Backend", company="Acme")["id"]
    cid = store.add_candidate(name="Maya Okafor")["id"]
    store.add_to_pipeline(cid, jid, "Interviewing")
    res = call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Hired")
    assert text(res).startswith("Waiting for the recruiter's review: Move \"Maya Okafor\" from Interviewing to Hired")
    assert store.pipeline(jid)[0]["stage"] == "Interviewing"
    call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Offer")  # in play: applies
    assert store.pipeline(jid)[0]["stage"] == "Offer"
    res = call(server, "update_job", job_id=jid, status="closed")
    assert "Waiting for the recruiter's review: Close Backend (Acme)" in text(res)
    assert store.get_job(jid)["status"] == "open"
    listed = text(call(server, "list_jobs"))
    assert "Waiting for the recruiter's review, not applied yet: Move" in listed and "Close Backend (Acme)" in listed
    res = call(server, "log_activity", candidate_id=cid, kind="note", summary="Do not contact")
    assert "Waiting for the recruiter's review: Log a note" in text(res)
    assert not [t for t in store.timeline(cid) if t["kind"] == "note"]
    groups = review.sessions(store)
    assert len(groups) == 1 and len(groups[0]["items"]) == 3
    r = c.post("/review/keep-session", data={"session": groups[0]["session"]}, follow_redirects=False)
    msg = flash_of(r)
    # The Hired move was queued while they were in Interviewing; they're in Offer now, so it isn't made.
    assert msg.startswith("Kept 2 changes") and "Left 1" in msg and "moved to Offer since" in msg
    assert store.get_job(jid)["status"] == "closed"
    assert [t["body"] for t in store.timeline(cid) if t["kind"] == "note"] == ["Do not contact"]
    assert not review.pending(store)
    # A kept close was the recruiter's own choice: Today doesn't ask "Right?" about it.
    assert not [i for i in _ai_items(store) if i.get("confirm_id") and "Backend" in i["title"]]


def test_cautious_kept_hire_is_confirmed(store):
    server = build_server(store)
    _cautious(store)
    jid = store.add_job("Backend")["id"]
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    store.add_to_pipeline(cid, jid, "Interviewing")
    rid = data(call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Hired"))["pending_review"]["id"]
    review.keep(store, rid)
    assert store.pipeline(jid)[0]["stage"] == "Hired"
    assert not [i for i in _ai_items(store) if "Ana Ruiz" in i["title"]]
    with pytest.raises(ValueError, match="already kept or passed"):
        review.keep(store, rid)


def test_the_same_waiting_change_sent_twice_is_one_row(store):
    server = build_server(store)
    _cautious(store)
    cid = store.add_candidate(name="Ana Ruiz", current_title="Engineer")["id"]
    a = data(call(server, "update_candidate", candidate_id=cid, current_title="CTO"))["pending_review"]["id"]
    b = data(call(server, "update_candidate", candidate_id=cid, current_title="CTO"))["pending_review"]["id"]
    assert a == b and review.count(store) == 1


@pytest.mark.parametrize("path", ["/review", "/changes", "/settings", "/"])
def test_new_pages_have_no_dashes(store, path):
    server = build_server(store)
    _cautious(store)
    cid = store.add_candidate(name="Ana Ruiz", current_title="Engineer")["id"]
    call(server, "update_candidate", candidate_id=cid, current_title="CTO")
    body = local_client(store).get(path).text
    assert "—" not in body and " – " not in body


# ------------------------------------------------------------ S items
def test_problem_report_scrubs_spelled_out_emails_and_short_surnames():
    from talent import recipes

    title, body = recipes.issue_text("Import broke", "Mail ana at acme dot com or bo [at] x [dot] io, ask Bo Li. "
                                     "Also cy(at)corp.com. I looked at it. Then it worked.", names=["Bo Li"])
    assert "acme" not in body and "[at]" not in body and "corp.com" not in body
    assert body.count("[email removed]") == 3
    assert "Li." not in body and "I looked at it. Then it worked." in body


def test_get_candidate_carries_the_data_note(store):
    cid = store.add_candidate(name="Quinn")["id"]
    assert data(call(build_server(store), "get_candidate", candidate_id=cid))["data_note"].startswith("(Candidate")
