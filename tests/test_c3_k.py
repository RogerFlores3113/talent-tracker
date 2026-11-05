"""Cycle 3, fixer K: AI-side re-review fixes (plain replies, aliases, stage outcomes, Today lines, undo)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from conftest import flash_of, local_client

from talent import actions
from talent.mcp_server import build_server
from talent.paths import backups_dir
from test_mcp import call, structured, text


def _ai_titles(store) -> list[str]:
    return [i["title"] for i in actions.action_items(store)["items"] if i["kind"] == "ai_change"]


def _hour_ago() -> str:
    return str(int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()))


# ------------------------------------------------------- 1. plain replies
def test_more_write_tools_lead_with_a_plain_sentence(store):
    server = build_server(store)
    cid = store.add_candidate(name="Priya Raman", location="Austin", about="Backend")["id"]
    res = call(server, "update_candidate", candidate_id=cid, current_title="Staff Engineer", location="")
    assert text(res).splitlines()[0] == f'Updated "Priya Raman" (id {cid}): title, location cleared.'
    assert structured(res)["current_title"] == "Staff Engineer" and structured(res)["changed"]
    same = call(server, "update_candidate", candidate_id=cid, current_title="Staff Engineer")
    assert text(same).startswith(f'Nothing to change on "Priya Raman" (id {cid})')

    job = store.add_job("Staff Backend Engineer", "Acme Fintech")
    res = call(server, "edit_job_stages", job_id=job["id"], add=["Take-home after Screening"])
    first = text(res).splitlines()[0]
    assert first.startswith("Changed the stages of Staff Backend Engineer (Acme Fintech): added Take-home.")
    assert "Stages now: Sourced, Contacted, Screening, Take-home," in first
    assert structured(res)["stages"][3] == "Take-home"

    todo = store.add_suggestion("Send the offer", candidate_id=cid, job_id=job["id"])
    res = call(server, "complete_suggestion", suggestion_id=todo["id"])
    assert text(res).splitlines()[0] == ('Marked the to-do done: "Send the offer" (about Priya Raman, '
                                         'Staff Backend Engineer (Acme Fintech)).')
    assert structured(res)["done_at"]
    again = call(server, "complete_suggestion", suggestion_id=todo["id"])
    assert text(again).startswith("That to-do was already done")


# ------------------------------------------------------------ 2. aliases
def test_note_and_body_aliases(store):
    server = build_server(store)
    cid = store.add_candidate(name="Dana Lee")["id"]
    jid = store.add_job("Designer")["id"]
    call(server, "add_to_pipeline", candidate_id=cid, job_id=jid, note="Referred by Sam")
    call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Screening", note="Strong portfolio")
    call(server, "log_activity", candidate_id=cid, kind="call", body="Talked about the role")
    bodies = [t["body"] for t in store.timeline(cid)]
    for b in ("Referred by Sam", "Strong portfolio", "Talked about the role"):
        assert b in bodies
    with pytest.raises(Exception, match="doesn't take note"):  # both at once: the alias isn't silently dropped
        call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Sourced", note="a", notes="b")


# ------------------------------------------------- 3. outcomes from the AI
def test_ai_sets_outcomes_only_on_stages_it_adds(store):
    server = build_server(store)
    jid = store.add_job("Backend")["id"]
    out = structured(call(server, "edit_job_stages", job_id=jid, add=[{"name": "Placed", "outcome": "hired"},
                                                                      "Parked"], outcomes={"Parked": "lost"}))
    assert out["stage_outcomes"]["Placed"] == "hired" and out["stage_outcomes"]["Parked"] == "lost"
    before = store.job_stages(jid)
    for kw in ({"outcomes": {"Screening": "lost"}}, {"outcomes": {"Rejected": "open"}},
               {"outcomes": {"Sourced": "hired"}}, {"rename": {"Offer": {"name": "Offer", "outcome": "hired"}}}):
        with pytest.raises(Exception, match=r"No change made\. Ask the recruiter to change what \w+ counts as in "
                                            r"the app\."):
            call(server, "edit_job_stages", job_id=jid, **kw)
    assert store.job_stages(jid) == before


# ------------------------------------------------------ 4. misleading replies
def test_failed_stage_edit_lists_the_stages_as_they_are(store):
    server = build_server(store)
    job = store.add_job("Backend", "Acme")
    with pytest.raises(Exception) as err:
        call(server, "edit_job_stages", job_id=job["id"], add=["Take-home"], order=["Take-home", "Nope"])
    msg = str(err.value)
    listed = msg.split("still has these stages: ")[1]
    assert "Take-home" not in listed and listed.startswith("Sourced, Contacted")
    assert "Its stages are" not in msg and "No change made" in msg
    assert [s["name"] for s in store.job_stages(job["id"])][:2] == ["Sourced", "Contacted"]


def test_kept_note_names_only_saved_values(store):
    server = build_server(store)
    maya = store.add_candidate(name="Maya Okafor", email="maya@x.com", source="Referral")
    res = call(server, "save_candidate", name="Maya Okafor", email="maya@x.com", phone="555 0100",
               linkedin_url="https://linkedin.com/in/maya", source="LinkedIn")
    out = structured(res)
    assert set(out["identity_kept"]) == {"phone", "linkedin_url"}
    assert out["kept"] == ["source"]  # source had a value and kept it; phone and LinkedIn were empty
    assert "these saved values were kept: source." in text(res)
    assert not store.get_candidate(maya["id"])["phone"]


# --------------------------------------------------------- 5. Today lines
def test_today_flags_ai_moves_to_offer_and_hired(store):
    server = build_server(store)
    job = store.add_job("Staff Backend Engineer", "Acme Fintech")
    ana, bo, cy = (store.add_candidate(name=n)["id"] for n in ("Ana Diaz", "Bo Li", "Cy Park"))
    for cid in (ana, bo, cy):
        store.add_to_pipeline(cid, job["id"], "Interviewing")
    call(server, "move_stage", candidate_id=ana, job_id=job["id"], stage="Offer")
    call(server, "move_stage", candidate_id=bo, job_id=job["id"], stage="Hired")
    call(server, "move_stage", candidate_id=cy, job_id=job["id"], stage="Screening")  # not worth a line
    titles = _ai_titles(store)
    assert "Your AI app moved Ana Diaz to Offer on Staff Backend Engineer (Acme Fintech). Review or undo." in titles
    assert "Your AI app marked Bo Li hired on Staff Backend Engineer (Acme Fintech). Right?" in titles  # c5 A
    assert not any("Cy Park" in t for t in titles)

    # Undone: the line goes. Moved by the recruiter since: it goes too.
    move = next(a for a in store.ai_actions() if a["kind"] == "stage_move" and a["candidate_name"] == "Bo Li")
    store.undo_ai_action(move["id"])
    store.move_stage(ana, job["id"], "Interviewing", by="app")
    assert not [t for t in _ai_titles(store) if "Ana Diaz" in t or "Bo Li" in t]


def test_today_drops_out_moves_the_recruiter_moved_back(store):
    server = build_server(store)
    jid = store.add_job("Data Lead")["id"]
    cid = store.add_candidate(name="Lee Chen")["id"]
    store.add_to_pipeline(cid, jid, "Screening")
    call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Rejected")
    assert "Your AI app moved 1 person out of Data Lead. Review or undo." in _ai_titles(store)
    store.move_stage(cid, jid, "Screening", by="app")
    store.move_stage(cid, jid, "Rejected", by="app")  # the recruiter agreed: their own move, no AI line
    assert not [t for t in _ai_titles(store) if "Data Lead" in t]


# ------------------------------------------------------------- 6. backups
def test_undoing_an_ai_added_person_backs_up_first(store):
    server = build_server(store)
    structured(call(server, "save_candidate", name="Lee Chen", email="lee@x.com"))
    action = next(a for a in store.ai_actions() if a["kind"] == "candidate")
    c = local_client(store)
    r = c.post(f"/changes/ai/{action['id']}/undo", follow_redirects=False)
    assert flash_of(r).startswith("Undid: Added Lee Chen")
    assert store.count_candidates() == 0
    assert any(f.name.startswith("before-delete-") for f in backups_dir().iterdir())


def test_undo_refused_when_the_backup_fails(store, monkeypatch):
    from talent import importers

    server = build_server(store)
    call(server, "save_candidate", name="Lee Chen", email="lee@x.com")
    action = next(a for a in store.ai_actions() if a["kind"] == "candidate")

    def broken(*a, **k):
        raise ValueError("Couldn't save a backup before making this change, so nothing was changed (disk full).")
    monkeypatch.setattr(importers, "backup_before_import", broken)
    r = local_client(store).post(f"/changes/ai/{action['id']}/undo", follow_redirects=False)
    assert "Couldn't save a backup" in flash_of(r, "flash_error") and store.count_candidates() == 1


# ------------------------------------------------- 7. undo from this row on
def test_undo_this_and_everything_after(store):
    server = build_server(store)
    jid = structured(call(server, "create_job", title="Designer"))["id"]
    cid = store.add_candidate(name="Ana Diaz")["id"]
    call(server, "add_to_pipeline", candidate_id=cid, job_id=jid)
    call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Screening")
    call(server, "update_job", job_id=jid, location="Remote")
    acts = store.ai_actions()  # newest first: job_edit, stage_move, pipeline_add, job_created
    add = next(a for a in acts if a["kind"] == "pipeline_add")
    c = local_client(store)
    page = c.get("/changes").text
    assert "Undo this and everything after" in page
    assert re.search(rf'action="/changes/ai/{add["id"]}/undo-from"[^>]*data-confirm="Undo 3 changes your AI app '
                     r'made, from this one to the newest\?', page)
    assert "—" not in page
    r = c.post(f"/changes/ai/{add['id']}/undo-from", follow_redirects=False)
    assert flash_of(r).startswith("Undid 3 AI changes"), flash_of(r)
    assert store.candidate_pipelines(cid) == [] and store.get_job(jid)["location"] is None
    left = {a["kind"]: a["undoable"] for a in store.ai_actions()}
    assert left["job_created"] is True  # older than the row: untouched
    r = c.post(f"/changes/ai/{add['id']}/undo-from", follow_redirects=False)
    assert "already undone" in flash_of(r)
    assert "gone" in flash_of(c.post("/changes/ai/99999/undo-from", follow_redirects=False), "flash_error")


# ---------------------------------------------------------- 8. nicknames
def test_nickname_with_last_name_is_an_exact_match(store):
    michael = store.add_candidate(name="Michael Okonkwo", current_title="Staff Engineer")
    store.add_candidate(name="Ada Okonkwo")
    store.add_candidate(name="Mike Brown")
    res = store.search_candidates("mike okonkwo")
    assert res.hits[0].candidate["id"] == michael["id"] and not res.hits[0].closest
    assert res.exact_total == 1
    assert [h.candidate["name"] for h in store.search_candidates("mike").hits] == ["Mike Brown"]  # alone: no
    assert store.search_candidates("bob okonkwo").exact_total == 0  # another first name: still not exact
