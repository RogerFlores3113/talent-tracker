"""Cycle 6 re-review, fixer J: copy follow-up steps between jobs, a reply offers the move to Screening, the
Shortlist's Keep all names the stage picked and shows its count, and a repeated batch says Already logged."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import pytest

from talent import actions, mcp_server, outreach, web
from talent.mcp_server import VIEW_KEY, build_server
from talent.mcp_views import view_html

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


@pytest.fixture(autouse=True)
def _apps_host(monkeypatch):
    monkeypatch.setattr(mcp_server, "_client_apps", lambda mcp: True)


def text(res):
    return "\n".join(c.text for c in res.content)


def stage_of(store, cid, jid):
    return next(p["stage"] for p in store.candidate_pipelines(cid) if p["job_id"] == jid)


def key_of(server, res):
    d = res.structuredContent or {}
    ids = [p["id"] for p in d.get("added") or d.get("prospects") or [] if p.get("status", "proposed") == "proposed"]
    return call(server, "shortlist_card_key", job_id=res.meta[VIEW_KEY]["job_id"],
                prospect_ids=ids).structuredContent["key"]


# ------------------------------------------------------------ 1. copy steps
def test_copy_steps_from_another_job(store):
    a = store.add_job("ML Engineer", company="Northwind")
    b = store.add_job("Data Engineer", company="Ledgerline")
    steps = [{"label": "InMail", "day": 0, "text": "Hi {first_name}, {job} at {company}?"},
             {"label": "Nudge", "day": 4, "text": "Following up on {job}"},
             {"label": "Last", "day": 9, "text": "Last note"}]
    outreach.save_sequence(store, a["id"], steps)
    c = local_client(store)
    page = c.get(f"/jobs/{b['id']}").text
    assert "Copy steps from another job" in page and "ML Engineer, Northwind (3 steps)" in page
    assert "Copy steps" not in c.get(f"/jobs/{a['id']}").text  # nothing else to copy from
    r = c.post(f"/jobs/{b['id']}/sequence/copy", data={"from_job_id": str(a["id"])}, follow_redirects=False)
    assert flash_of(r).startswith("Copied 3 steps from ML Engineer.")
    got = outreach.sequence(store, b["id"])["steps"]
    assert [(s["label"], s["day"]) for s in got] == [("InMail", 0), ("Nudge", 4), ("Last", 9)]
    assert outreach.draft(got[0], {"name": "Dev Raman"}, store.get_job(b["id"])) == \
        "Hi Dev, Data Engineer at Ledgerline?"
    assert outreach.sequence(store, a["id"])["steps"] == got  # the source is unchanged
    # Replacing steps it already has asks first; bad picks say why.
    assert "Replace this job's steps" in c.get(f"/jobs/{b['id']}").text
    r = c.post(f"/jobs/{b['id']}/sequence/copy", data={"from_job_id": str(b["id"])}, follow_redirects=False)
    assert "another job" in flash_of(r, "flash_error")
    empty = store.add_job("Recruiter")
    r = c.post(f"/jobs/{b['id']}/sequence/copy", data={"from_job_id": str(empty["id"])}, follow_redirects=False)
    assert "no follow-up steps" in flash_of(r, "flash_error")
    page = c.get(f"/jobs/{b['id']}").text
    assert "—" not in page and "–" not in page


# ------------------------------------------------------------ 2. a reply offers the next stage
def test_reply_offers_move_to_screening_and_moves_nobody(store):
    job = store.add_job("ML Engineer", company="Northwind")
    dev = store.add_candidate(name="Dev Raman")
    store.add_to_pipeline(dev["id"], job["id"])
    server = build_server(store)
    call(server, "log_activity", candidate_id=dev["id"], kind="message", summary="InMail", job_id=job["id"])
    assert stage_of(store, dev["id"], job["id"]) == "Contacted"
    res = call(server, "log_activity", candidate_id=dev["id"], kind="reply", summary="Keen to chat")
    assert stage_of(store, dev["id"], job["id"]) == "Contacted"  # never moved by itself
    assert "Ask the user whether to move them to Screening" in text(res) and "{" not in text(res).split("move_stage")[0]
    assert res.structuredContent["suggested_step"] == {"tool": "move_stage", "candidate_id": dev["id"],
                                                       "job_id": job["id"], "stage": "Screening"}
    item = next(i for i in actions.action_items(store)["items"] if i["kind"] == "replied")
    assert item["title"] == "Dev Raman replied. Move them to Screening?" and item["next_stage"] == "Screening"
    assert "still in Contacted" in item["detail"]
    c = local_client(store)
    home = c.get("/").text
    assert "Replied" in home and "Move to Screening" in home
    assert "move them to Screening (move_stage)" in text(call(server, "list_action_items"))
    # Pressing it moves them, and the offer goes.
    r = c.post("/actions/advance", data={"candidate_id": dev["id"], "job_id": job["id"], "stage": "Screening"},
               follow_redirects=False)
    assert "Moved Dev Raman to Screening" in flash_of(r)
    assert not [i for i in actions.action_items(store)["items"] if i["kind"] == "replied"]
    assert outreach.reply_stage(store, dev["id"], job["id"]) is None


def test_they_replied_in_the_app_points_to_the_move(store):
    job = store.add_job("ML Engineer", company="Northwind")
    maya = store.add_candidate(name="Maya Okafor")
    store.add_to_pipeline(maya["id"], job["id"])
    outreach.save_sequence(store, job["id"], list(outreach.DEFAULT_STEPS))
    store.log(maya["id"], "message", "InMail", job_id=job["id"])
    store.move_stage(maya["id"], job["id"], "Contacted", by="app")
    c = local_client(store)
    r = c.post(f"/jobs/{job['id']}/followups/{maya['id']}/replied", follow_redirects=False)
    assert "Move to Screening is on Today" in flash_of(r)
    assert stage_of(store, maya["id"], job["id"]) == "Contacted"
    jp = c.get(f"/jobs/{job['id']}").text
    assert "replied" in jp and 'name="stage" value="Screening"' in jp and "Move to Screening" in jp
    # From the candidate page too.
    bo = store.add_candidate(name="Bo Li")
    store.add_to_pipeline(bo["id"], job["id"])
    r = c.post(f"/candidates/{bo['id']}/log", data={"kind": "reply", "body": "Yes please", "job_id": str(job["id"])},
               follow_redirects=False)
    assert "Move to Screening is on Today" in flash_of(r)
    assert stage_of(store, bo["id"], job["id"]) == "Sourced"


# ------------------------------------------------------------ 3 and 4. Keep all
def test_keep_all_confirm_names_the_stage_and_counts(store):
    job = store.add_job("ML Engineer", company="Northwind")
    call(build_server(store), "propose_candidates", job_id=job["id"], people=[
        {"name": n, "title": "ML Engineer", "company": "Acme",
         "linkedin_url": f"https://www.linkedin.com/in/{n.lower().replace(' ', '-')}/"}
        for n in ("Ana Ruiz", "Bo Li", "Cy Park")])
    page = local_client(store).get(f"/jobs/{job['id']}/shortlist").text
    assert 'data-confirm="Keep all 3 and add them to {stage}?' in page and "Keep all (3)" in page
    js = (Path(web.__file__).parent / "static" / "app.js").read_text()
    assert 'split("{stage}")' in js and "select[name=stage]" in js
    html = view_html("shortlist")
    assert '"Keep all ("+n+")"' in html and "keep-all" in html and 'replied:"Replied"' in view_html("today")


def test_keep_prospects_grammar_one_needs_your_choice(store):
    job = store.add_job("ML Engineer", company="Northwind")
    store.add_candidate(name="Maya Okafor", current_company="Ledgerline")
    server = build_server(store)
    res = call(server, "propose_candidates", job_id=job["id"], people=[
        {"name": "Maya Okafor", "title": "ML Engineer", "company": "Acme"},
        {"name": "Bo Li", "title": "ML Engineer", "company": "Acme",
         "linkedin_url": "https://www.linkedin.com/in/bo-li/"}])
    ids = [p["id"] for p in res.structuredContent["added"]]
    kept = call(server, "keep_prospects", job_id=job["id"], prospect_ids=ids, view_key=key_of(server, res))
    assert '1 needs your choice first: "Maya Okafor".' in text(kept)


# ------------------------------------------------------------ 5. a repeated batch
def test_repeat_batch_says_already_logged(store):
    job = store.add_job("ML Engineer", company="Northwind")
    ids = [store.add_candidate(name=n)["id"] for n in ("Ana Ruiz", "Bo Li", "Cy Park")]
    for cid in ids:
        store.add_to_pipeline(cid, job["id"])
    server = build_server(store)
    call(server, "log_activity", candidate_ids=ids[:2], kind="message", summary="InMail", job_id=job["id"])
    again = call(server, "log_activity", candidate_ids=ids[:2], kind="message", summary="InMail", job_id=job["id"])
    assert text(again).startswith('Already logged a message with 2 people today ("Ana Ruiz", "Bo Li"), so nothing '
                                  "new was logged.")
    assert again.structuredContent["already"] == 2
    assert len([t for t in store.timeline(ids[0]) if t["kind"] == "message"]) == 1
    assert len([a for a in store.ai_actions() if a["kind"] == "outreach_batch"]) == 1
    # Mixed: the new person is logged and moved, the others are named as already logged.
    mixed = call(server, "log_activity", candidate_ids=ids, kind="message", summary="InMail")
    first = text(mixed).splitlines()[0]
    assert first.startswith('Logged a message with 1 person ("Cy Park")')
    assert 'Already logged today for 2, so not again: "Ana Ruiz", "Bo Li".' in first
    assert stage_of(store, ids[2], job["id"]) == "Contacted"
    # A different kind, or one dated another day, is logged as usual.
    email = call(server, "log_activity", candidate_ids=ids[:2], kind="email", summary="Email", job_id=job["id"])
    assert text(email).startswith("Logged an email with 2 people")
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    back = call(server, "log_activity", candidate_ids=ids[:2], kind="message", summary="Older", when=old,
                job_id=job["id"])
    assert text(back).startswith("Logged a message with 2 people")
