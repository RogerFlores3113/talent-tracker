"""Cycle 4, fixer S: the sourcing Shortlist (propose, keep, pass, undo) and batch save_candidates."""
import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from talent import actions, shortlist
from talent.mcp_server import INSTRUCTIONS, build_server
from talent.store import _MIGRATIONS

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def data(res):
    return res.structuredContent


def text(res):
    return "\n".join(c.text for c in res.content)


def _people(*names):
    return [{"name": n, "linkedin_url": f"https://www.linkedin.com/in/{n.lower().replace(' ', '-')}/",
             "title": "Backend Engineer", "company": "Acme", "location": "Austin", "fit": f"{n} built payments APIs"}
            for n in names]


def _ai_actions(store, kind):
    return [a for a in store.ai_actions() if a["kind"] == kind]


def test_v7_adds_the_prospects_table(store):
    assert store.schema_version == len(_MIGRATIONS) >= 7
    cols = {r["name"] for r in store._rows("PRAGMA table_info(prospects)")}
    assert {"job_id", "linkedin_url", "name", "title", "company", "location", "fit", "source", "proposed_by",
            "status", "pass_reason", "candidate_id", "created_at", "decided_at"} <= cols


def test_propose_adds_skips_flags_and_logs_one_action(store):
    job = store.add_job("Backend Engineer", company="Acme")
    other = store.add_job("Platform Lead", company="Lumen")
    wei = store.add_candidate(name="Wei Zhang", linkedin_url="https://linkedin.com/in/wei-zhang")
    store.add_to_pipeline(wei["id"], other["id"], "Screening")
    lee = store.add_candidate(name="Lee Chen", linkedin_url="https://linkedin.com/in/lee-chen")
    store.add_to_pipeline(lee["id"], job["id"])
    server = build_server(store)
    people = _people("Ana Ruiz", "Wei Zhang", "Lee Chen") + [
        {"name": "Ana Ruiz", "linkedin_url": "https://linkedin.com/in/ana-ruiz?trk=x"},
        {"name": "Sam Lead", "linkedin_url": "https://www.linkedin.com/sales/lead/123"}]
    res = call(server, "propose_candidates", job_id=job["id"], people=people)
    d, t = data(res), text(res)
    assert t.startswith("Added 2 people to the Shortlist for Backend Engineer (Acme)")
    assert [a["name"] for a in d["added"]] == ["Ana Ruiz", "Wei Zhang"]
    assert d["added"][0]["linkedin_url"] == "https://linkedin.com/in/ana-ruiz"
    wei_row = d["added"][1]
    assert wei_row["checks"]["saved"]["id"] == wei["id"]
    assert "Already saved as Wei Zhang (id %d). In Platform Lead, Lumen: Screening" % wei["id"] in t
    reasons = {x["name"]: x["reason"] for x in d["skipped"]}
    assert reasons["Lee Chen"].startswith("already in this job's pipeline")
    assert reasons["Ana Ruiz"] == "listed twice in this call"
    assert "linkedin.com/in/" in reasons["Sam Lead"]
    logged = _ai_actions(store, "prospects_proposed")
    assert len(logged) == 1 and logged[0]["summary"] == "Proposed 2 people for the Shortlist of Backend Engineer (Acme)"
    # A second session doesn't re-add them.
    again = data(call(server, "propose_candidates", job_id=job["id"], people=_people("Ana Ruiz")))
    assert again["added"] == [] and again["skipped"][0]["reason"] == "already on this Shortlist"
    assert len(_ai_actions(store, "prospects_proposed")) == 1  # nothing added, nothing logged


def test_propose_refuses_more_than_25(store):
    job = store.add_job("Backend")
    server = build_server(store)
    with pytest.raises(ToolError, match="at most 25"):
        call(server, "propose_candidates", job_id=job["id"], people=_people(*[f"P{i} X" for i in range(26)]))
    assert store._rows("SELECT * FROM prospects") == []


def test_passed_people_are_remembered_for_the_job_and_flagged_elsewhere(store):
    job = store.add_job("Backend Engineer", company="Acme")
    other = store.add_job("SRE", company="Acme")
    server = build_server(store)
    call(server, "propose_candidates", job_id=job["id"], people=_people("Ana Ruiz", "Bo Li"))
    ana = next(p for p in shortlist.listed(store, job["id"]) if p["name"] == "Ana Ruiz")
    c = local_client(store)
    r = c.post(f"/jobs/{job['id']}/shortlist/{ana['id']}/pass", data={"reason": "Too junior"}, follow_redirects=False)
    assert "Passed on Ana Ruiz" in flash_of(r)
    res = data(call(server, "propose_candidates", job_id=job["id"], people=_people("Ana Ruiz")))
    assert res["skipped"][0]["reason"] == "passed on for this job (Too junior)"
    res = call(server, "propose_candidates", job_id=other["id"], people=_people("Ana Ruiz"))
    assert "Passed on for Backend Engineer, Acme: Too junior" in text(res)
    passed = data(call(server, "list_prospects", job_id=job["id"], status="passed"))
    assert [p["name"] for p in passed["prospects"]] == ["Ana Ruiz"] and passed["prospects"][0]["pass_reason"] == "Too junior"
    waiting = call(server, "list_prospects", job_id=job["id"])
    assert "1 person waiting on the Shortlist" in text(waiting) and "Bo Li" in text(waiting)


def test_undo_takes_back_only_the_waiting_people_of_that_batch(store):
    job = store.add_job("Backend Engineer", company="Acme")
    server = build_server(store)
    call(server, "propose_candidates", job_id=job["id"], people=_people("Ana Ruiz", "Bo Li", "Cy Park"))
    rows = {p["name"]: p for p in shortlist.listed(store, job["id"])}
    c = local_client(store)
    c.post(f"/jobs/{job['id']}/shortlist/{rows['Ana Ruiz']['id']}/keep")
    c.post(f"/jobs/{job['id']}/shortlist/{rows['Bo Li']['id']}/pass")
    action = _ai_actions(store, "prospects_proposed")[0]
    assert "Proposed 3 people for the Shortlist of Backend Engineer (Acme)" in c.get("/changes").text
    r = c.post(f"/changes/ai/{action['id']}/undo", follow_redirects=False)
    assert "Undid: Proposed 3 people" in flash_of(r) and "left 2 you already kept or passed" in flash_of(r)
    left = {p["name"]: p["status"] for p in shortlist.listed(store, job["id"], "all")}
    assert left == {"Ana Ruiz": "kept", "Bo Li": "passed"}
    assert any(a["candidate_name"] == "Ana Ruiz" for a in store.pipeline(job["id"]))


def test_keep_saves_or_links_and_adds_to_the_first_stage(store):
    job = store.add_job("Backend Engineer", company="Acme")
    wei = store.add_candidate(name="Wei Zhang", linkedin_url="https://linkedin.com/in/wei-zhang")
    store.add_candidate(name="Bo Li", current_company="Globex")
    server = build_server(store)
    call(server, "propose_candidates", job_id=job["id"], people=_people("Ana Ruiz", "Wei Zhang", "Bo Li"))
    rows = {p["name"]: p for p in shortlist.listed(store, job["id"])}
    c = local_client(store)
    page = c.get(f"/jobs/{job['id']}/shortlist").text
    assert "Ana Ruiz built payments APIs" in page and "Same as Bo Li, Globex" in page and "Someone new" in page  # c6 labels
    assert "—" not in page and "–" not in page
    r = c.post(f"/jobs/{job['id']}/shortlist/{rows['Ana Ruiz']['id']}/keep", follow_redirects=False)
    assert flash_of(r) == "Saved Ana Ruiz and added them to Sourced."
    ana = store.find_duplicate(linkedin_url="https://linkedin.com/in/ana-ruiz")
    assert ana["current_company"] == "Acme" and ana["source"] == "LinkedIn"
    assert any("Why they fit: Ana Ruiz built payments APIs" in (t["body"] or "") for t in store.timeline(ana["id"]))
    r = c.post(f"/jobs/{job['id']}/shortlist/{rows['Wei Zhang']['id']}/keep", follow_redirects=False)
    assert "linked to the person you already had" in flash_of(r)
    assert store._row("SELECT candidate_id FROM prospects WHERE id = ?", (rows["Wei Zhang"]["id"],))["candidate_id"] == wei["id"]
    # Same name, no link match: the recruiter must choose.
    r = c.post(f"/jobs/{job['id']}/shortlist/{rows['Bo Li']['id']}/keep", follow_redirects=False)
    assert "Choose the same person or someone new" in flash_of(r, "flash_error")
    r = c.post(f"/jobs/{job['id']}/shortlist/{rows['Bo Li']['id']}/keep", data={"as_new": "1"}, follow_redirects=False)
    assert flash_of(r) == "Saved Bo Li and added them to Sourced."
    assert len(store.find_by_name("Bo Li")) == 2
    assert {a["candidate_name"] for a in store.pipeline(job["id"])} == {"Ana Ruiz", "Wei Zhang", "Bo Li"}
    assert all(a["stage"] == "Sourced" for a in store.pipeline(job["id"]))


def test_keep_all_pass_all_put_back_badge_and_today(store):
    job = store.add_job("Backend Engineer", company="Acme")
    store.add_candidate(name="Cy Park")
    server = build_server(store)
    call(server, "propose_candidates", job_id=job["id"], people=_people("Ana Ruiz", "Bo Li", "Cy Park"))
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "shortlist"]
    assert items[0]["title"] == "Your AI app proposed 3 people for Backend Engineer, Acme"
    c = local_client(store)
    assert "Your AI app proposed 3 people for Backend Engineer, Acme" in c.get("/").text
    job_page = c.get(f"/jobs/{job['id']}").text
    assert f'href="/jobs/{job["id"]}/shortlist"' in job_page and '<span class="badge shortlist">3</span>' in job_page
    assert "3 on the Shortlist" in c.get("/jobs").text
    page = c.get(f"/jobs/{job['id']}/shortlist").text
    assert 'data-confirm="Keep all 3' in page and 'data-confirm="Pass on all 3?' in page
    r = c.post(f"/jobs/{job['id']}/shortlist/keep-all", follow_redirects=False)
    assert flash_of(r) == ("Kept 2 people and added them to Sourced. You can undo this on Changes. 1 need your "
                           "choice first: Cy Park.")
    r = c.post(f"/jobs/{job['id']}/shortlist/pass-all", data={"reason": "Not now"}, follow_redirects=False)
    assert "Passed on 1 person" in flash_of(r)
    assert not [i for i in actions.action_items(store)["items"] if i["kind"] == "shortlist"]
    cy = shortlist.listed(store, job["id"], "passed")[0]
    assert cy["pass_reason"] == "Not now"
    c.post(f"/jobs/{job['id']}/shortlist/{cy['id']}/put-back")
    assert [p["name"] for p in shortlist.listed(store, job["id"])] == ["Cy Park"]


def test_the_ai_can_only_propose(store, monkeypatch):
    from talent import mcp_server
    assert "keep_prospect" not in {t.name for t in anyio.run(build_server(store).list_tools)}  # c6 A: plain client
    monkeypatch.setattr(mcp_server, "_client_apps", lambda mcp: True)
    tools = {t.name: t for t in anyio.run(build_server(store).list_tools)}
    assert {"propose_candidates", "list_prospects", "save_candidates"} <= set(tools)
    # Cycle 5: Keep and Pass exist only for the Shortlist card's buttons, hidden from the model (MCP Apps
    # visibility "app") and refused without the card's key (tests/test_c5_e.py). Cycle 6 A: listed only to hosts
    # that say they show views, with the card's own key tool.
    app_only = [n for n in tools if (tools[n].meta or {}).get("ui", {}).get("visibility") == ["app"]]
    assert sorted(app_only) == ["keep_prospect", "keep_prospects", "pass_prospect", "pass_prospects", "shortlist_card_key"]
    assert not [n for n in tools if n not in app_only and any(w in n for w in ("keep", "pass", "delete", "remove"))]
    assert tools["propose_candidates"].meta["ui"]["resourceUri"].endswith("shortlist.html")
    assert "propose_candidates" in INSTRUCTIONS and "only when the recruiter asks you to save" in INSTRUCTIONS
    assert "use propose_candidates instead" in tools["save_candidates"].description
    assert "instead of saving them" in tools["propose_candidates"].description


def test_save_candidates_batches_with_the_same_rules_and_undo_all(store):
    job = store.add_job("Backend Engineer", company="Acme")
    store.add_candidate(name="Wei Zhang", linkedin_url="https://linkedin.com/in/wei-zhang", current_title="SWE")
    store.add_candidate(name="Bo Li", email="bo@old.com")
    store.add_candidate(name="Kim Ode", email="kim@x.com")
    server = build_server(store)
    res = call(server, "save_candidates", job_id=job["id"], stage="contact", people=[
        {"name": "Ana Ruiz", "linkedin_url": "https://linkedin.com/in/ana-ruiz", "title": "Staff Engineer",
         "skills": ["Go"], "hobby": "chess"},
        {"name": "Wei Zhang", "linkedin_url": "https://www.linkedin.com/in/wei-zhang/", "current_title": "Staff SWE"},
        {"name": "Bo Li", "email": "bo@new.com"},
        {"name": "Tom Ng", "email": "kim@x.com"},
    ])
    d, t = data(res), text(res)
    # Cycle 4 W: a batch merge keeps a saved title (Wei stays "SWE"), so Wei is "already saved".
    assert t.startswith("Saved 1 new person, 1 already saved with nothing new, and 2 need the recruiter's answer "
                        "before saving; "
                        "added 2 to Backend Engineer (Acme) in Contacted.")
    assert [r["name"] for r in d["saved"]] == ["Ana Ruiz"] and d["saved"][0]["ignored_fields"] == ["hobby"]
    assert [r["name"] for r in d["needs_answer"]] == ["Bo Li", "Tom Ng"]
    assert "already belongs to Kim Ode" in d["needs_answer"][1]["reason"]
    ana = store.find_duplicate(linkedin_url="https://linkedin.com/in/ana-ruiz")
    assert ana["current_title"] == "Staff Engineer" and ana["tags"] == ["Go"]
    assert {a["candidate_name"]: a["stage"] for a in store.pipeline(job["id"])} == {"Ana Ruiz": "Contacted",
                                                                                     "Wei Zhang": "Contacted"}
    assert store.find_by_name("Tom Ng") == []
    c = local_client(store)
    r = c.post("/changes/undo-ai", data={"since": "1"}, follow_redirects=False)
    assert "Undid 3 AI changes" in flash_of(r)
    assert store.pipeline(job["id"]) == [] and store.find_by_name("Ana Ruiz") == []
    assert store.find_by_name("Wei Zhang")[0]["current_title"] == "SWE"


def test_save_candidates_limits(store):
    server = build_server(store)
    with pytest.raises(ToolError, match="at most 25"):
        call(server, "save_candidates", people=[{"name": f"P{i}"} for i in range(26)])
    with pytest.raises(ToolError, match="stage needs a job_id"):
        call(server, "save_candidates", people=[{"name": "Ana"}], stage="Sourced")
    assert store.count_candidates() == 0
