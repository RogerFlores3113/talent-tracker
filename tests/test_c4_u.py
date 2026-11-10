"""Cycle 4, fixer U: stage outcomes, stage sets, who you hire for, status text, search and paste skills."""
from datetime import datetime, timedelta, timezone

import pytest

from talent import actions
from talent.importers import quick_parse
from talent.mcp_server import build_server
from talent.store import stage_outcome

from conftest import flash_of, local_client
from test_mcp import call, structured, text


def _at(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(second=0, microsecond=0)


def _people(store, job_id, stage, *names):
    ids = []
    for n in names:
        cid = store.add_candidate(name=n)["id"]
        store.add_to_pipeline(cid, job_id, stage)
        ids.append(cid)
    return ids


# ------------------------------------------------------ 1. outcome inference
@pytest.mark.parametrize("name", ["Client passed", "Passed", "Declined", "Not selected", "Client rejected",
                                  "Withdrew", "Client declined", "Client pass", "Not progressed",
                                  "Hiring team passed", "Candidate withdrew", "Offer declined", "Rejected by HM"])
def test_exit_names_count_as_out(name):
    assert stage_outcome(name) == "lost"


@pytest.mark.parametrize("name", ["Screen passed", "Passed phone screen", "Screened", "Submitted", "Client interview",
                                  "Offer"])
def test_working_names_stay_in_play(name):
    assert stage_outcome(name) == "open"


def test_ai_custom_agency_stages_count_client_passed_as_out(store):
    server = build_server(store)
    out = call(server, "create_job", title="VP Sales", company="Pinecrest",
               stages=["Sourced", "Screened", "Submitted to client", "Client interview", "Placed", "Client passed",
                       "Withdrew"])
    data = structured(out)
    assert data["stage_outcomes"]["Client passed"] == "lost" and data["stage_outcomes"]["Withdrew"] == "lost"
    assert "Placed counts as hired; Client passed and Withdrew count as out." in text(out)


# ------------------------------------------------------ 2. agency stage set
def test_agency_set_keeps_screened_out_of_client_updates(store):
    assert actions.STAGE_SETS["agency"][1][-2] == "Client passed"
    job = store.add_job("VP Sales", company="Harbor", stages=actions.stage_set("agency"))
    assert {s["name"]: s["outcome"] for s in store.job_stages(job["id"])}["Client passed"] == "lost"
    _people(store, job["id"], "Screened", "Marcus Bell", "Tom Nguyen")
    assert actions.saved_omit(store, job["id"]) == ["Sourced", "Screened"]
    out = actions.job_status_text(store, job["id"])
    assert "Marcus" not in out and "No one to share yet." in out
    _people(store, job["id"], "Submitted", "Priya Raman")
    out = actions.job_status_text(store, job["id"])
    assert "Submitted (1): Priya Raman" in out and "Marcus" not in out
    plain = store.add_job("Backend", stages=["Sourced", "Phone screen", "Onsite", "Hired"])
    assert actions.saved_omit(store, plain["id"]) == ["Sourced"]  # no Submitted stage: only the internal names


# ------------------------------------------------- 3. stage sets and copies
def test_create_job_takes_a_stage_set_or_another_jobs_stages(store):
    server = build_server(store)
    agency = structured(call(server, "create_job", title="Exec", stage_set="agency"))
    assert agency["stages"] == list(actions.STAGE_SETS["agency"][1])
    custom = store.add_job("Founding Eng", stages=["Reached out", "Intro call", "Take-home", "Hired",
                                                   {"name": "Parked", "outcome": "lost"}])
    copy = structured(call(server, "create_job", title="Second Eng", stages_from_job_id=custom["id"]))
    assert copy["stages"] == ["Reached out", "Intro call", "Take-home", "Hired", "Parked"]
    assert copy["stage_outcomes"]["Parked"] == "lost"
    for bad, msg in (({"stage_set": "fancy"}, "No stage set called"), ({"stages_from_job_id": 999}, "No job with id 999"),
                     ({"stage_set": "agency", "stages": ["A", "B"]}, "Give only one")):
        with pytest.raises(Exception, match=msg):
            call(server, "create_job", title="X", **bad)
    tools = {t.name: t for t in __import__("anyio").run(server.list_tools)}
    assert "Prefer a built-in stage_set" in tools["create_job"].description


def test_new_job_form_copies_stages_from_a_job(store):
    custom = store.add_job("Founding Eng", company="Us", stages=["Reached out", "Intro call", "Hired", "Passed"])
    c = local_client(store)
    form = c.get("/jobs/new").text
    assert 'name="copy_from"' in form and "Founding Eng, Us" in form
    r = c.post("/jobs", data={"title": "Second Eng", "stage_set": "default", "copy_from": str(custom["id"])},
               follow_redirects=False)
    jid = int(r.headers["location"].split("/jobs/")[1].split("?")[0])
    assert [s["name"] for s in store.job_stages(jid)] == ["Reached out", "Intro call", "Hired", "Passed"]


# --------------------------------------------------- 4. who you hire for
def test_welcome_and_settings_ask_who_you_hire_for(store):
    c = local_client(store)
    home = c.get("/").text
    assert "Are you hiring for your own company or for clients?" in home and 'action="/jobs/wording"' in home
    r = c.post("/jobs/wording", data={"inhouse": "1", "next": "/"}, follow_redirects=False)
    assert r.headers["location"].split("?")[0] == "/" and actions.inhouse(store) and "in-house" in flash_of(r)
    r = c.post("/jobs/wording", data={"inhouse": "1", "next": "https://evil.example"}, follow_redirects=False)
    assert r.headers["location"].split("?")[0] == "/jobs"
    page = c.get("/settings").text
    assert "Who you hire for" in page and 'name="hiring_for" value="own" checked' in page
    c.post("/settings", data={"hiring_for": "clients", "pipeline_view": "board"})
    assert not actions.inhouse(store)
    c.post("/settings", data={"hiring_for": "own", "pipeline_view": "board"})
    assert actions.inhouse(store)


def test_inhouse_wording_reaches_the_ai_and_the_job_page(store):
    job = store.add_job("Backend", company="Lumen", stages=actions.stage_set("agency"))
    marcus = _people(store, job["id"], "Submitted", "Marcus Bell")[0]
    store.move_stage(marcus, job["id"], "Client passed")
    actions.set_inhouse(store, True)
    server = build_server(store)
    digest = call(server, "status_digest").content[0].text
    assert digest.startswith("Hiring update for all open jobs")
    assert "Client passed" not in digest and "Marcus" not in digest  # ended stages are left out of the chat digest
    page = local_client(store).get(f"/jobs/{job['id']}").text
    assert "<summary>Status update</summary>" in page and "for the client" not in page
    actions.set_inhouse(store, False)
    assert call(server, "status_digest").content[0].text.startswith("Status update for all open jobs")


# ------------------------------------------------------------ 5. status text
def test_not_shown_line_is_for_the_recruiter_only(store):
    job = store.add_job("Backend", company="Acme")
    _people(store, job["id"], "Sourced", "S1", "S2")
    _people(store, job["id"], "Screening", "Ana Ruiz")
    assert "Not shown" not in actions.job_status_text(store, job["id"])
    server = build_server(store)
    out = call(server, "job_pipeline", job_id=job["id"])
    assert "Not shown" not in out.content[0].text
    assert "Private to you, not part of the update: Not shown: Sourced (2)." in out.content[1].text
    digest = call(server, "status_digest")
    assert "Not shown" not in digest.content[0].text and "Backend, Acme: not shown: Sourced (2)" in digest.content[1].text
    page = local_client(store).get(f"/jobs/{job['id']}").text
    assert "Only you see this: not shown: Sourced (2)." in page
    assert 'data-copy-text="' in page and "Not shown" not in page.split('data-copy-text="')[1].split('"')[0]


def test_rollup_counts_people_not_applications(store):
    a = store.add_job("Backend", company="Harbor")
    b = store.add_job("Data", company="Harbor")
    store.add_job("Ops", company="Other")
    priya = _people(store, a["id"], "Screening", "Priya")[0]
    store.add_to_pipeline(priya, b["id"], "Screening")
    _people(store, b["id"], "Screening", "Bo")
    harbor = next(r for r in actions.company_rollup(store) if r["company"] == "Harbor")
    assert harbor["active"] == 2


def test_counts_as_change_shows_notice_logs_and_warns_about_interviews(store):
    job = store.add_job("VP Sales", company="Pinecrest", stages=["Sourced", "Client interview", "Admin", "Placed",
                                                                 "Rejected"])
    marcus = _people(store, job["id"], "Admin", "Marcus Bell")[0]
    store.log(marcus, "interview", "Final", at=_at(3), job_id=job["id"])
    c = local_client(store)
    r = c.post(f"/jobs/{job['id']}/stages", data={"op": "outcome", "stage": "Admin", "outcome": "lost"},
               follow_redirects=False)
    assert f"ask_cancel={marcus}" in r.headers["location"]
    note = flash_of(r)
    assert "Admin now counts as Out." in note and "Marcus Bell still has an interview coming up." in note
    page = c.get(r.headers["location"]).text
    assert "Cancel it</button>" in page
    changes = c.get("/changes").text
    assert "Admin on VP Sales (Pinecrest) counts as Out (was In play)" in changes
    cid = actions.stage_outcome_changes(store)[0]["id"]
    r = c.post(f"/changes/stage-outcome/{cid}/undo", follow_redirects=False)
    assert "Admin counts as In play again" in flash_of(r)
    assert {s["name"]: s["outcome"] for s in store.job_stages(job["id"])}["Admin"] == "open"
    r = c.post(f"/changes/stage-outcome/{cid}/undo", follow_redirects=False)
    assert "already undone" in flash_of(r, "flash_error")
    r = c.post(f"/jobs/{job['id']}/stages", data={"op": "outcome", "stage": "Sourced", "outcome": "hired"},
               follow_redirects=False)
    assert r.headers["location"].startswith(f"/jobs/{job['id']}/edit") and "Nobody in it" in flash_of(r)


# ------------------------------------------------------------------ 6. search
def test_found_via_is_searchable_and_hits_show_jobs(store):
    dana = store.add_candidate(name="Dana Okafor", source="Referral from Alex")
    store.add_candidate(name="Alex Kim", source="LinkedIn")
    job = store.add_job("Founding Backend", company="Us")
    store.add_to_pipeline(dana["id"], job["id"], "Contacted")
    names = [h.candidate["name"] for h in store.search_candidates("referral alex").hits]
    assert names[0] == "Dana Okafor"
    server = build_server(store)
    out = call(server, "search_candidates", query="referral")
    assert "Dana Okafor" in text(out) and "Found via: Referral from Alex" in text(out)
    assert f"In: Founding Backend (Us) [job_id {job['id']}, " in text(out) and "], Contacted" in text(out)
    hit = structured(out)["results"][0]
    assert [{k: v for k, v in j.items() if k != "since"} for j in hit["jobs"]] == [{"job_id": job["id"], "job": "Founding Backend (Us)", "stage": "Contacted", "stage_rank": 1}]
    only = structured(call(server, "search_candidates", query="", source="linkedin"))["results"]
    assert [r["name"] for r in only] == ["Alex Kim"]
    table = local_client(store).get("/candidates?q=referral").text
    assert "Dana Okafor" in table and "Found via: Any source" in table


def test_a_job_title_as_job_id_gets_a_plain_error(store):
    server = build_server(store)
    cid = store.add_candidate(name="Ana")["id"]
    with pytest.raises(Exception, match=r"No change made\. job_id is a number, not 'Staff ML'; list_jobs shows them\."):
        call(server, "add_to_pipeline", candidate_id=cid, job_id="Staff ML")
    with pytest.raises(Exception, match=r"^job_id is a number"):
        call(server, "job_pipeline", job_id="Staff ML")
    job = store.add_job("Staff ML")
    assert 'Added "Ana"' in text(call(server, "add_to_pipeline", candidate_id=cid, job_id=str(job["id"])))


# ------------------------------------------------------------ 7. paste skills
def test_paste_never_puts_links_in_skills():
    out = quick_parse("Priyanka Desai\nML Engineer at Acme\nSkills\nPyTorch, Kubernetes, Spark, Node.js, Socket.io, CI/CD\n"
                      "linkedin.com/in/priyanka-desai-ml\nhttps://github.com/pd")
    assert out["tags"] == ["PyTorch", "Kubernetes", "Spark", "Node.js", "Socket.io", "CI/CD"]


# --------------------------------------------------------- 8. match highlights
def test_match_line_highlights_every_word(store):
    store.add_candidate(name="Rae", location="Austin", tags=["Kubernetes"], about="Runs GPU clusters at scale.")
    hit = store.search_candidates("kubernetes austin").hits[0]
    assert "\x02Kubernetes\x03" in hit.snippet and "\x02Austin\x03" in hit.snippet
    out = text(call(build_server(store), "search_candidates", query="kubernetes austin"))
    assert "**Kubernetes**" in out and "**Austin**" in out
    one = store.search_candidates("gpu").hits[0]
    assert one.snippet.count("\x02") == 1 and " · " not in one.snippet
