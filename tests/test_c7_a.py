"""Cycle 7, fixer A: submit_to_client fixes its own gaps (stage, person), a fuller write-up, the Client desk page and
tool, and small client wording bugs."""
from datetime import datetime, timedelta, timezone

import anyio
from mcp.types import CallToolResult

from talent import actions, desk, offers, outreach, reports
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


def _age(store, cid, jid, days):
    at = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    with store._lock, store._conn:
        store._conn.execute("UPDATE applications SET stage_changed_at = ? WHERE candidate_id = ? AND job_id = ?",
                            (at, cid, jid))


def test_new_jobs_for_an_agency_get_a_submitted_stage(store):
    server = build_server(store)
    actions.set_inhouse(store, False)  # said: I hire for clients
    job = data(call(server, "create_job", title="Controller", company="Pinecrest Capital"))
    assert "Submitted" in job["stages"]


def test_submit_to_client_adds_the_stage_and_the_person_and_says_so(store):
    job = store.add_job("Staff Backend", company="Pinecrest Capital", hiring_manager="Sam Lee",
                        hiring_manager_email="sam@pinecrest.com")
    cid = store.add_candidate(name="Lena Fox", current_title="Backend Engineer", current_company="Stripe",
                              about="Ten years of payments systems work.", tags="go, postgres")["id"]
    server = build_server(store)
    res = call(server, "submit_to_client", candidate_id=cid, job_id=job["id"],
               why_fit="Ran payments at scale", note="Happy to talk Friday")
    t = text(res)
    assert "added Submitted to client" in t and "wasn't on the job yet" in t
    d = data(res)
    assert d["moved"] and d["stage"] == "Submitted to client" and len(d["also_did"]) == 2
    body = d["writeup"]
    for bit in ("Why they fit: Ran payments at scale", "Happy to talk Friday", "About: Ten years of payments",
                "Backend Engineer at Stripe", "go"):
        assert bit in body
    assert "—" not in t and "–" not in t
    assert any(a["kind"] == "stages_edit" for a in store.ai_actions())


def _desk_store(store):
    job = store.add_job("Staff Backend", company="Harbor Freight Labs", stages=actions.stage_set("agency"),
                        hiring_manager="Dana Reyes", hiring_manager_email="dana@hfl.com")
    other = store.add_job("Ops", company="Pinecrest Capital", stages=actions.stage_set("agency"))
    a = store.add_candidate(name="Lena Hoffmann")["id"]
    b = store.add_candidate(name="Bo Li")["id"]
    store.add_to_pipeline(a, job["id"], "Submitted")
    _age(store, a, job["id"], 8)
    store.add_to_pipeline(b, other["id"], "Submitted")
    _age(store, b, other["id"], 9)
    reports.save_client(store, "harbor freight labs", update_day="Monday")
    return job, other, a, b


def test_client_desk_merges_the_week_for_one_client(store):
    job, other, a, b = _desk_store(store)
    c = store.add_candidate(name="Cy Park")["id"]
    store.add_to_pipeline(c, job["id"], "Client interview")
    offers.save(store, c, job["id"], status="out", answer_by=(datetime.now() + timedelta(days=1)).date().isoformat())
    d = desk.desk(store, "harbor freight labs")
    by = {s["kind"]: s for s in d["sections"]}
    assert [s["kind"] for s in d["sections"]] == ["update", "chase", "feedback", "offer", "followup"]
    assert by["chase"]["count"] == 1 and "Lena Hoffmann" in by["chase"]["items"][0]["text"]
    assert by["chase"]["items"][0]["to"] == "dana@hfl.com"
    assert by["offer"]["count"] == 1 and "Cy Park" in by["offer"]["items"][0]["title"]
    assert "Bo Li" not in desk.desk_text(d)  # another client's people stay off this desk


def test_client_desk_page_and_mark_chased_returns_to_the_desk(store):
    job, other, a, b = _desk_store(store)
    cl = local_client(store)
    slug = reports.slug("harbor freight labs")
    html = cl.get(f"/clients/{slug}/desk").text
    assert "Harbor Freight Labs desk" in html and "Lena Hoffmann" in html and "Mark as chased" in html
    assert "—" not in html and "–" not in html
    assert f"/clients/{slug}/desk" in cl.get(f"/clients/{slug}").text
    r = cl.post(f"/clients/{slug}/chased", data={"manager": "dana reyes", "manager_name": "Dana Reyes",
                                                  "back": "desk"}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/clients/{slug}/desk")
    assert "Dana Reyes" in flash_of(r)


def test_client_desk_follow_ups_and_tool(store, monkeypatch):
    job, other, a, b = _desk_store(store)
    outreach.save_sequence(store, job["id"], [{"label": "First", "day": 0, "text": "Hi {first_name}"},
                                              {"label": "Nudge", "day": 2, "text": "Checking in"}])
    import json
    long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat()
    with store._lock, store._conn:
        store._conn.execute("UPDATE settings SET value = ? WHERE key = ?", (json.dumps(
            {"steps": outreach.sequence(store, job["id"])["steps"], "since": long_ago}),
            f"{outreach.SEQ_PREFIX}{job['id']}"))
    d = store.add_candidate(name="Ann Wu")["id"]
    store.add_to_pipeline(d, job["id"], "Sourced")
    first = (datetime.now(timezone.utc) - timedelta(days=4)).replace(microsecond=0).isoformat()
    store.log(d, "message", "Hello", at=first, job_id=job["id"])
    server = build_server(store)
    res = call(server, "client_desk", company="harbor")
    sections = {s["kind"]: s for s in data(res)["sections"]}
    assert sections["followup"]["count"] == 1 and "Ann Wu" in text(res)
    assert sections["chase"]["items"][0]["to"] == "dana@hfl.com"
    page = local_client(store).get(f"/clients/{reports.slug('harbor freight labs')}/desk").text
    assert f"/jobs/{job['id']}/followups/{d}/sent" in page


def test_company_works_for_client_tools(store):
    _desk_store(store)
    server = build_server(store)
    assert "Lena Hoffmann" in text(call(server, "client_chase", company="harbor"))
    res = call(server, "update_client", company="harbor freight labs", update_day="friday")
    assert "Changed" in text(res)


def test_changes_say_update_day_and_reply_in_words(store):
    _desk_store(store)
    server = build_server(store)
    call(server, "update_client", client="harbor freight labs", update_day="friday", time_zone="PT")
    cand = store.add_candidate(name="Ann Wu")["id"]
    call(server, "log_activity", candidate_id=cand, kind="reply", summary="Interested!")
    page = local_client(store).get("/changes").text
    assert "update day, time zone" in page and "update_day" not in page
    assert "Logged a reply from Ann Wu" in page


def test_submit_to_client_in_cautious_mode_holds_the_new_stage(store):
    from talent import review
    review.set_cautious(store, True)
    job = store.add_job("Controller", company="Pinecrest Capital")
    cid = store.add_candidate(name="Lena Fox")["id"]
    res = call(build_server(store), "submit_to_client", candidate_id=cid, job_id=job["id"])
    assert "Cautious mode" in text(res) and data(res)["pending_review"]
    assert "Submitted to client" not in [s["name"] for s in store.job_stages(job["id"])]
    assert not store.candidate_pipelines(cid)  # nothing else landed
