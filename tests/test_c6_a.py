"""Cycle 6, fixer A: the last AI gaps (where updates get emailed, card keys, Undo all AI and Review, the AI's
to-dos) and the AI session ledger with Roll back this session."""
import json
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from talent import actions, backups, mcp_server, review, sessions, shortlist
from talent.mcp_server import AI_TODO_CAP, NOT_FROM_VIEW, VIEW_KEY, build_server

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res) -> str:
    return "\n".join(c.text for c in res.content)


def hour_ago() -> str:
    return str(int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()))


def why_backups() -> list[str]:
    return [b["why"] for b in backups.list_backups()]


@pytest.fixture
def cautious(store):
    review.set_cautious(store, True)
    return store


# ------------------------------------------------- 1. where updates get emailed
def test_cautious_holds_a_new_hiring_manager_email_on_update_and_create(cautious):
    store = cautious
    job = store.add_job("Senior Backend Engineer", company="Acme Payments")
    server = build_server(store)
    res = call(server, "update_job", job_id=job["id"], hiring_manager_email="dana@attacker.test", location="Remote")
    assert "Held for your review" in text(res) and res.structuredContent["pending_review"]
    j = store.get_job(job["id"])
    assert j["hiring_manager_email"] is None and j["location"] == "Remote"  # the rest still lands
    assert "attacker" not in local_client(store).get("/clients/acme-payments").text
    made = call(server, "create_job", title="Data Lead", company="Acme Payments", hiring_manager_email="x@evil.test")
    new_id = made.structuredContent["id"]
    assert store.get_job(new_id)["hiring_manager_email"] is None and "Held for you" in text(made)
    page = local_client(store).get("/review").text
    assert "Client updates for this job can be emailed to this address." in page and "dana@attacker.test" in page
    # Kept on Review: it lands, and it counts as checked (no "Changed by your AI app" note).
    rid = res.structuredContent["pending_review"]["id"]
    review.keep(store, rid)
    assert store.get_job(job["id"])["hiring_manager_email"] == "dana@attacker.test"
    assert not review.email_changes(store, [job["id"]])


def test_an_ai_set_email_is_pointed_out_until_kept(store):
    job = store.add_job("Backend", company="Acme")
    call(build_server(store), "update_job", job_id=job["id"], hiring_manager_email="dana@acme.test")
    c = local_client(store)
    for path in (f"/jobs/{job['id']}", "/clients/acme"):
        assert "Changed by your AI app" in c.get(path).text, path
    item = next(i for i in actions.action_items(store)["items"] if i["key"].startswith("aiemail:"))
    assert item["title"] == "Your AI app set where updates for Backend (Acme) go: dana@acme.test."
    e = review.email_changes(store)[0]
    r = c.post(f"/jobs/{job['id']}/ai-email/{e['id']}/keep", follow_redirects=False)
    assert r.status_code == 303 and "Kept the hiring manager email" in flash_of(r)
    assert "Changed by your AI app" not in c.get(f"/jobs/{job['id']}").text
    assert not [i for i in actions.action_items(store)["items"] if i["key"].startswith("aiemail:")]


def test_hold_new_emails_and_phones_option(cautious):
    store = cautious
    lee = store.add_candidate(name="Lee Chen", linkedin_url="https://linkedin.com/in/lee-chen")
    bo = store.add_candidate(name="Bo Li")
    server = build_server(store)
    call(server, "update_candidate", candidate_id=bo["id"], phone="555 0100")
    assert store.get_candidate(bo["id"])["phone"] == "555 0100"  # off by default: a fill lands
    review.set_options(store, ai_hold_contacts=True)
    res = call(server, "update_candidate", candidate_id=lee["id"], email="lee@evil.test", location="Austin")
    cand = store.get_candidate(lee["id"])
    assert cand["email"] is None and cand["location"] == "Austin" and "Held for you" in text(res)
    res = call(server, "save_candidate", name="Lee Chen", linkedin_url="https://linkedin.com/in/lee-chen",
               email="lee2@evil.test")
    assert store.get_candidate(lee["id"])["email"] is None and res.structuredContent["pending_review"]
    review.set_cautious(store, False)  # the option only counts in Cautious mode
    cy = store.add_candidate(name="Cy Park")
    call(server, "update_candidate", candidate_id=cy["id"], email="cy@x.test")
    assert store.get_candidate(cy["id"])["email"] == "cy@x.test"


def test_hold_every_stage_move_option_and_the_settings_copy(cautious):
    store = cautious
    job = store.add_job("Backend")
    ana = store.add_candidate(name="Ana Ruiz")
    store.add_to_pipeline(ana["id"], job["id"])
    server = build_server(store)
    call(server, "move_stage", candidate_id=ana["id"], job_id=job["id"], stage="Offer")
    assert store.pipeline(job["id"])[0]["stage"] == "Offer"  # not held by default
    c = local_client(store)
    r = c.post("/settings/cautious", data={"ai_cautious": "1", "ai_hold_moves": "1"}, follow_redirects=False)
    assert r.status_code == 303 and review.hold_moves(store) and not review.hold_contacts(store)
    res = call(server, "move_stage", candidate_id=ana["id"], job_id=job["id"], stage="Screening")
    assert store.pipeline(job["id"])[0]["stage"] == "Offer" and "Waiting" in text(res)
    page = c.get("/settings").text
    assert "Hold new emails and phones too" in page and "Hold every stage move" in page
    assert "a move to Offer included, are not held unless you turn on Hold every stage move" in page
    assert "—" not in page and "–" not in page


# ------------------------------------------------------------- 2. card keys
def test_card_key_never_reaches_the_model_and_plain_clients_get_no_card_tools(store, monkeypatch):
    job = store.add_job("Backend")
    server = build_server(store)
    res = call(server, "propose_candidates", job_id=job["id"], people=[
        {"name": "Pat Prospect", "linkedin_url": "https://linkedin.com/in/pat"}])
    pid = res.structuredContent["added"][0]["id"]
    assert res.meta[VIEW_KEY] == {"job_id": job["id"]}
    names = {t.name for t in anyio.run(server.list_tools)}
    assert not names & {"keep_prospect", "pass_prospect", "shortlist_card_key"}
    assert call(server, "shortlist_card_key", job_id=job["id"], prospect_ids=[pid]).isError
    assert text(call(server, "keep_prospect", job_id=job["id"], prospect_id=pid, view_key="x")) == NOT_FROM_VIEW
    assert shortlist.counts(store, job["id"])["proposed"] == 1
    view = mcp_server.view_html("shortlist")
    assert "shortlist_card_key" in view and "k.key" not in view


def _card(store, monkeypatch, *names):
    monkeypatch.setattr(mcp_server, "_client_apps", lambda mcp: True)
    job = store.add_job("Backend", company="Ledgerly")
    server = build_server(store)
    res = call(server, "propose_candidates", job_id=job["id"],
               people=[{"name": n, "linkedin_url": f"https://linkedin.com/in/{n.split()[0].lower()}"} for n in names])
    pids = [a["id"] for a in res.structuredContent["added"]]
    key = call(server, "shortlist_card_key", job_id=job["id"], prospect_ids=pids).structuredContent["key"]
    return server, job, pids, key


def test_card_keeps_and_passes_are_ai_actions_that_undo_all_ai_takes_back(store, monkeypatch):
    server, job, (pat, bo), key = _card(store, monkeypatch, "Pat Prospect", "Bo Li")
    call(server, "keep_prospect", job_id=job["id"], prospect_id=pat, view_key=key)
    key = call(server, "shortlist_card_key", job_id=job["id"], prospect_ids=[bo]).structuredContent["key"]  # one-time
    call(server, "pass_prospect", job_id=job["id"], prospect_id=bo, view_key=key)
    kinds = [a["kind"] for a in store.ai_actions()]
    assert kinds[:2] == ["card_pass", "card_keep"]
    page = local_client(store).get("/changes").text
    assert page.count("on the Shortlist card in your AI app, for Backend (Ledgerly)") == 4  # keep and pass, rows and session
    assert "from the Shortlist for Backend" not in page
    c = local_client(store)
    r = c.post(f"/changes/ai/{store.ai_actions()[0]['id']}/undo", follow_redirects=False)  # the pass, alone
    assert flash_of(r).startswith('Undid: Passed on "Bo Li"') and shortlist.counts(store, job["id"])["passed"] == 0
    r = c.post("/changes/undo-ai", data={"since": hour_ago()}, follow_redirects=False)
    msg = flash_of(r)
    assert msg.startswith('Undid 2 AI changes: Kept "Pat Prospect" on the Shortlist card'), msg
    assert store.count_candidates() == 0 and shortlist.counts(store, job["id"]) == {"proposed": 0, "kept": 0,
                                                                                   "passed": 0}


# ------------------------------------------------- 3. Undo all AI and Review
def test_undo_all_ai_passes_queued_items_and_leaves_kept_ones(cautious):
    store = cautious
    ana = store.add_candidate(name="Ana Ruiz", current_title="Engineer")
    bo = store.add_candidate(name="Bo Li", current_title="Analyst")
    server = build_server(store)
    kept = call(server, "update_candidate", candidate_id=ana["id"], current_title="Staff Engineer")
    review.keep(store, kept.structuredContent["pending_review"]["id"])
    call(server, "update_candidate", candidate_id=bo["id"], current_title="CTO")  # still waiting
    call(server, "save_candidate", name="Zed Mallory", email="zed@x.test")  # lands
    assert store._row("SELECT confirmed_at FROM ai_actions WHERE kind = 'profile'")["confirmed_at"]
    c = local_client(store)
    assert "and 1 waiting on Review is passed" in c.get("/changes").text
    r = c.post("/changes/undo-ai", data={"since": hour_ago()}, follow_redirects=False)
    msg = flash_of(r)
    assert "Undid 1 AI change: Added Zed Mallory." in msg and "Passed 1 change that waited on Review" in msg
    assert "Left 1 you kept on Review or on Today as it is" in msg and "A backup from just before" in msg
    assert review.count(store) == 0 and store.get_candidate(ana["id"])["current_title"] == "Staff Engineer"
    assert store.get_candidate(bo["id"])["current_title"] == "Analyst"
    r = c.post("/changes/undo-ai", data={"window": "24", "include_kept": "1"}, follow_redirects=False)
    assert "Undid 1 AI change" in flash_of(r) and store.get_candidate(ana["id"])["current_title"] == "Engineer"
    assert why_backups().count("Before undoing AI changes") == 2


def test_a_field_only_undo_all_and_a_keep_all_take_a_backup(cautious):
    store = cautious
    ana = store.add_candidate(name="Ana Ruiz", current_title="Engineer")
    server = build_server(store)
    call(server, "update_candidate", candidate_id=ana["id"], current_title="CTO")
    c = local_client(store)
    r = c.post("/review/keep-session", data={"session": review.pending(store)[0]["session"]}, follow_redirects=False)
    assert "Kept 1 change" in flash_of(r) and "Before Keep all on Review" in why_backups()
    review.set_cautious(store, False)
    call(server, "update_candidate", candidate_id=ana["id"], location="Austin")
    c.post("/changes/undo-ai", data={"since": hour_ago()}, follow_redirects=False)
    assert "Before undoing AI changes" in why_backups()


# --------------------------------------------------------------- 4. to-dos
def test_the_ai_completes_only_its_own_to_dos(store):
    server = build_server(store)
    mine = store.add_suggestion("Follow up with Noah Williams")
    with pytest.raises(ToolError, match="your own to-do"):
        call(server, "complete_suggestion", suggestion_id=mine["id"])
    assert not store._row("SELECT done_at FROM suggestions WHERE id = ?", (mine["id"],))["done_at"]
    its = call(server, "add_suggestion", text="Ping Lee").structuredContent
    assert "Marked the to-do done" in text(call(server, "complete_suggestion", suggestion_id=its["id"]))
    review.set_cautious(store, True)
    res = call(server, "complete_suggestion", suggestion_id=mine["id"])
    assert "Waiting for your review: Mark the to-do \"Follow up with Noah Williams\" done" in text(res)
    assert "To-do done" in local_client(store).get("/review").text
    review.keep(store, res.structuredContent["pending_review"]["id"])
    assert store._row("SELECT done_at FROM suggestions WHERE id = ?", (mine["id"],))["done_at"]


def test_add_suggestion_has_an_hourly_cap(store):
    server = build_server(store)
    for i in range(AI_TODO_CAP):
        call(server, "add_suggestion", text=f"To-do {i}")
    with pytest.raises(ToolError, match=f"already added {AI_TODO_CAP} to-dos this hour"):
        call(server, "add_suggestion", text="Wire the deposit")
    assert not store._row("SELECT 1 FROM suggestions WHERE text = 'Wire the deposit'")


# ------------------------------------------------------- 5. session ledger
def test_each_session_takes_a_backup_at_its_first_write_and_logs_its_actions(store):
    one, two = build_server(store), build_server(store)
    call(one, "list_jobs")
    assert not sessions._all(store) and not backups.list_backups()  # reading writes nothing
    call(one, "save_candidate", name="Ana Ruiz", email="ana@x.test")
    call(one, "save_candidate", name="Bo Li", email="bo@x.test")
    call(two, "create_job", title="Backend")
    assert why_backups().count("At the start of an AI session") == 2
    recs = {len(r["actions"]): r for r in sessions._all(store)}
    assert set(recs) == {1, 2} and all(r["backup"] for r in recs.values())
    kinds = {a["id"]: a["kind"] for a in store.ai_actions()}
    assert [kinds[i] for i in recs[2]["actions"]] == ["candidate", "candidate"]
    assert [kinds[i] for i in recs[1]["actions"]] == ["job_created"]


def test_roll_back_one_session_undoes_only_its_writes_and_passes_its_queue(store):
    lee = store.add_candidate(name="Lee Chen", current_title="SWE")
    bad, good = build_server(store), build_server(store)
    call(good, "save_candidate", name="Ana Ruiz", email="ana@x.test")
    call(bad, "update_candidate", candidate_id=lee["id"], location="Mars")
    call(bad, "save_candidate", name="Zed Mallory", email="zed@x.test")
    review.set_cautious(store, True)
    call(bad, "update_candidate", candidate_id=lee["id"], current_title="Hacker")
    zed = store.find_duplicate("zed@x.test")
    store.log(zed["id"], "note", "Called them")  # the recruiter worked on Zed: Zed stays
    listed = sessions.listing(store)
    target = next(g for g in listed if g["waiting"])
    assert target["can_roll_back"] and target["undoable"] == 1  # Zed has the recruiter's note
    c = local_client(store)
    page = c.get("/changes").text
    assert "AI sessions" in page and page.count("Roll back this session") == 2
    assert "Roll back this session" in c.get("/review").text
    r = c.post("/changes/session/roll-back", data={"session": target["id"]}, follow_redirects=False)
    msg = flash_of(r)
    assert msg.startswith("Rolled back the AI session: undid 1 change"), msg
    assert "passed 1 that waited on Review" in msg and "Couldn't undo 1: Added Zed Mallory" in msg
    assert store.get_candidate(lee["id"])["location"] is None and store.get_candidate(lee["id"])["current_title"] == "SWE"
    assert store.find_duplicate("ana@x.test") and review.count(store) == 0  # the other session's write stays
    assert "Before rolling back an AI session" in why_backups()
    rec = sessions.get(store, target["id"])
    assert rec["rolled_back"]["undone"] == 1 and rec["rolled_back"]["left"]
    assert "—" not in c.get("/changes").text


def test_roll_back_leaves_what_the_recruiter_kept_and_the_ai_cannot_roll_back(cautious):
    store = cautious
    ana = store.add_candidate(name="Ana Ruiz", current_title="Engineer")
    server = build_server(store)
    res = call(server, "update_candidate", candidate_id=ana["id"], current_title="Staff Engineer")
    review.keep(store, res.structuredContent["pending_review"]["id"])
    sid = review.decided(store)[0]["session"]
    out = sessions.roll_back(store, sid)
    assert out["undone"] == [] and store.get_candidate(ana["id"])["current_title"] == "Staff Engineer"
    names = {t.name for t in anyio.run(server.list_tools)}
    assert not [n for n in names if "roll" in n or "session" in n]


def test_a_failed_session_backup_does_not_stop_the_ai_app(store, monkeypatch):
    from talent import importers

    def boom(*a, **k):
        raise ValueError("Couldn't save a backup")
    monkeypatch.setattr(importers, "backup_before_import", boom)
    res = call(build_server(store), "save_candidate", name="Ana Ruiz", email="ana@x.test")
    assert res.structuredContent["created"]
    rec = sessions._all(store)[0]
    assert rec["backup"] is None and rec["backup_error"] and len(rec["actions"]) == 1
    assert json.loads(store._row("SELECT value FROM settings WHERE key LIKE 'ai_session:%'")["value"])["id"]


# ------------------------------------------------------- 6. keys, folder, privacy
def test_flash_health_and_cards_use_their_own_subkeys():
    from talent import paths, web
    key = paths.app_key()
    assert len({paths.subkey(key, p) for p in ("flash", "health", "cards")}) == 3
    import hashlib
    import hmac
    raw = "abc"
    assert web._flash_sig(key, raw) != hmac.new(key.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    assert web.decode_flash(web.encode_flash(key, {"notice": "Hi"}), key) == {"notice": "Hi"}


def test_only_ours_matches_exact_names(tmp_path):
    from talent import paths
    folder = tmp_path / "d"
    folder.mkdir()
    (folder / "talent.db").write_text("")
    (folder / "talent.db-wal").write_text("")
    assert paths._only_ours(folder)
    (folder / "backups-old").mkdir()
    assert not paths._only_ours(folder)


def test_privacy_page_names_a_remote_search_index_and_says_backups_are_plain(store, monkeypatch):
    c = local_client(store)
    page = c.get("/privacy").text
    assert "Backups are plain copies of the database" in page and "Search index: remote" not in page
    monkeypatch.setenv("TYPESENSE_URL", "https://search.example.com")
    monkeypatch.setenv("TYPESENSE_API_KEY", "k")
    page = c.get("/privacy").text
    assert "Search index: remote, search.example.com." in page
    assert "—" not in page and "–" not in page
