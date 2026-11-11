"""Cycle 18 must-fix bugs: the chat export with no job leaves out rejected and withdrawn people like the Share page,
the Submit-to-client write-up follows the Share file's rule (no private notes, comp or contact details unless asked;
nothing for someone out of the job), and an interview logged with no job never shows in a job's client update."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import local_client
from test_c13_c import read_xlsx

from talent import actions, chase, xlsx_export
from talent.mcp_server import build_server

STAGES = ["Sourced", "Screening", "Submitted to client", "Interview", "Hired", "Rejected", "Withdrawn"]


def _tool(store, name):
    return build_server(store)._tool_manager._tools[name].fn


def _names(book):
    rows = book["Candidates"]["rows"]
    at = next(i for i, r in enumerate(rows) if r and r[0] == "Name")
    return [r[0] for r in rows[at + 1:]]


def _world(store):
    job = store.add_job("Platform Engineer", "Acme", stages=STAGES)
    other = store.add_job("Designer", "Acme")
    ana = store.add_candidate(
        name="Ana In Play", email="ana@x.com", phone="415-555-0100",
        about="Strong. Email ana.secret@gmail.com or (415) 555-0134. Rejected after onsite; failed system design.",
        work_history=[{"title": "Engineer", "company": "Beta (call +44 20 7946 0958)", "start": "2019", "end": "2023"}])
    rex = store.add_candidate(name="Rex Rejected", email="rex@x.com")
    wil = store.add_candidate(name="Wil Withdrawn", email="wil@x.com")
    out = store.add_candidate(name="Otto Elsewhere", email="otto@x.com")
    store.add_to_pipeline(ana["id"], job["id"], "Screening")
    store.add_to_pipeline(rex["id"], job["id"], "Rejected")
    store.add_to_pipeline(wil["id"], job["id"], "Withdrawn")
    store.add_to_pipeline(out["id"], other["id"], "Sourced")
    store.log(ana["id"], "note", "Wants $240k base, can start in January. Private: weak on testing.", job_id=job["id"])
    return job, other, ana, rex, wil, out


# ------------------------------------------------- 1. chat export with no job
def test_chat_export_with_no_job_leaves_out_rejected_and_withdrawn(store, tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(xlsx_export, "save_to_downloads",
                        lambda data, name: saved.setdefault("data", data) and tmp_path / name)
    job, other, ana, rex, wil, out = _world(store)
    res = _tool(store, "export_candidates_xlsx")(candidate_ids=[p["id"] for p in (ana, rex, wil, out)])
    assert _names(read_xlsx(saved["data"])) == ["Ana In Play", "Otto Elsewhere"]
    assert res.structuredContent["people"] == 2
    assert "Left out 2 people rejected or withdrawn" in res.content[0].text


def test_chat_export_and_web_share_leave_out_the_same_people(store, tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(xlsx_export, "save_to_downloads",
                        lambda data, name: saved.setdefault("data", data) and tmp_path / name)
    job, other, ana, rex, wil, out = _world(store)
    ids = [p["id"] for p in (ana, rex, wil, out)]
    _tool(store, "export_candidates_xlsx")(candidate_ids=ids)
    web = local_client(store).post("/candidates/share", data={"ids": ",".join(map(str, ids)),
                                                              "leave_rej": [str(rex["id"]), str(wil["id"])]})
    assert _names(read_xlsx(saved["data"])) == _names(read_xlsx(web.content))


def test_chat_export_can_opt_in_to_rejected_people(store, tmp_path, monkeypatch):
    monkeypatch.setattr(xlsx_export, "save_to_downloads", lambda data, name: tmp_path / name)
    job, other, ana, rex, wil, out = _world(store)
    res = _tool(store, "export_candidates_xlsx")(candidate_ids=[ana["id"], rex["id"], wil["id"]], include_rejected=True)
    assert res.structuredContent["people"] == 3


def test_chat_export_refuses_a_file_with_nobody_in_it(store, tmp_path, monkeypatch):
    monkeypatch.setattr(xlsx_export, "save_to_downloads", lambda data, name: tmp_path / name)
    job, other, ana, rex, wil, out = _world(store)
    with pytest.raises(Exception, match="rejected or withdrawn"):
        _tool(store, "export_candidates_xlsx")(candidate_ids=[rex["id"], wil["id"]])


def test_share_page_calls_a_withdrawn_person_withdrawn(store):
    job, other, ana, rex, wil, out = _world(store)
    html = local_client(store).get(f"/candidates/share?ids={rex['id']},{wil['id']}").text
    assert "Wil Withdrawn is marked withdrawn in Platform Engineer" in html
    assert "Rex Rejected is marked rejected in Platform Engineer" in html
    assert "Leave Wil Withdrawn out because they are withdrawn" in html


# ------------------------------------------------- 2. submit-to-client write-up
def _submit_html(store, job, cand, query=""):
    return local_client(store).get(f"/jobs/{job['id']}/submit/{cand['id']}{query}").text


def test_submit_writeup_has_no_notes_comp_or_contact_by_default(store):
    job, other, ana, *_ = _world(store)
    d = chase.writeup(store, ana["id"], job["id"])["body"]
    assert "ana.secret@gmail.com" not in d and "555-0134" not in d and "7946" not in d
    assert "ana@x.com" not in d and "415-555-0100" not in d
    assert "weak on testing" not in d and "Comp:" not in d and "240k" not in d and "From my screen" not in d
    assert "(email removed)" in d and "(phone removed)" in d
    html = _submit_html(store, job, ana)
    assert "weak on testing" not in html and "Comp:" not in html and "ana.secret@gmail.com" not in html


def test_submit_page_lists_what_is_in_and_out_above_the_writeup(store):
    job, other, ana, *_ = _world(store)
    html = _submit_html(store, job, ana)
    assert html.index("Read it over") < html.index('id="writeup-text"')
    assert html.index("Left out:") < html.index('id="writeup-text"')
    assert "Include my notes" in html and "Include contact details" in html


def test_submit_notes_and_contact_are_separate_opt_ins(store):
    job, other, ana, *_ = _world(store)
    notes = _submit_html(store, job, ana, "?notes=1")
    assert "weak on testing" in notes and "Comp: " in notes and "ana@x.com" not in notes
    assert "ana.secret@gmail.com" not in notes
    contact = _submit_html(store, job, ana, "?contact=1")
    assert "ana@x.com" in contact and "ana.secret@gmail.com" in contact and "weak on testing" not in contact


@pytest.mark.parametrize("who,word", [("rex", "rejected"), ("wil", "withdrawn")])
def test_submit_page_for_someone_out_of_the_job_says_so(store, who, word):
    job, other, ana, rex, wil, out = _world(store)
    person = {"rex": rex, "wil": wil}[who]
    html = _submit_html(store, job, person)
    assert f"is {word} in Platform Engineer" in html and "nothing to send" in html
    assert 'id="writeup-text"' not in html


def test_submit_page_for_someone_not_in_the_job_says_so(store):
    job, other, ana, rex, wil, out = _world(store)
    res = local_client(store).get(f"/jobs/{job['id']}/submit/{out['id']}")
    assert res.status_code == 200 and "isn&#39;t in Platform Engineer" in res.text and 'id="writeup-text"' not in res.text


def test_submit_to_client_tool_follows_the_same_rule(store):
    job, other, ana, rex, wil, out = _world(store)
    res = _tool(store, "submit_to_client")(candidate_id=ana["id"], job_id=job["id"], why_fit="Great fit")
    body = res.structuredContent["writeup"]
    assert "weak on testing" not in body and "Comp:" not in body and "ana.secret@gmail.com" not in body
    assert "ana@x.com" not in body and "Great fit" in body
    with pytest.raises(Exception, match="withdrawn"):
        _tool(store, "submit_to_client")(candidate_id=wil["id"], job_id=job["id"])
    with pytest.raises(Exception, match="rejected"):
        _tool(store, "submit_to_client")(candidate_id=rex["id"], job_id=job["id"])


# ------------------------------------------------- 3. an interview with no job
def _arjun(store):
    j1 = store.add_job("Backend", "Acme", stages=["Sourced", "Screening", "Interview", "Hired", "Rejected"])
    j2 = store.add_job("Data Lead", "Acme", stages=["Sourced", "Screening", "Interview", "Hired", "Rejected"])
    arjun = store.add_candidate(name="Arjun Rao")
    store.add_to_pipeline(arjun["id"], j1["id"], "Screening")
    store.add_to_pipeline(arjun["id"], j2["id"], "Sourced")
    return j1, j2, arjun


def _soon():
    return (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()


def test_an_interview_with_no_job_never_pulls_someone_into_a_jobs_client_update(store):
    j1, j2, arjun = _arjun(store)
    store.log(arjun["id"], "interview", "Onsite: coding", at=_soon(), job_id=None)
    text2 = actions.job_status_text(store, j2["id"])
    assert "Arjun" not in text2 and "Next interviews" not in text2
    assert "Next interviews" not in actions.job_status_text(store, j1["id"])
    assert actions.next_interviews(store, j1["id"]) == {} and actions.next_interviews(store, j2["id"]) == {}
    digest = _tool(store, "status_digest")()
    assert "Next interviews" not in digest.content[0].text and "Sourced" not in digest.content[0].text
    assert "since an interview is booked" not in repr(digest.structuredContent)


def test_an_interview_for_a_job_still_counts_for_that_job_only(store):
    j1, j2, arjun = _arjun(store)
    store.log(arjun["id"], "interview", "Onsite: coding", at=_soon(), job_id=j1["id"])
    assert "Next interviews" in actions.job_status_text(store, j1["id"])
    assert "Arjun" not in actions.job_status_text(store, j2["id"])


def test_an_interview_booked_for_a_left_out_stage_still_lists_that_person(store):
    j1, j2, arjun = _arjun(store)
    store.log(arjun["id"], "interview", "Screen", at=_soon(), job_id=j2["id"])
    text = actions.job_status_text(store, j2["id"])
    assert "Arjun" in text and "Next interviews" in text


def _form(store, arjun):
    return local_client(store).get(f"/candidates/{arjun['id']}").text


def test_interview_form_defaults_to_the_one_job(store):
    job = store.add_job("Backend", "Acme")
    arjun = store.add_candidate(name="Arjun Rao")
    store.add_to_pipeline(arjun["id"], job["id"], "Screening")
    html = _form(store, arjun)
    assert f'<option value="{job["id"]}" selected>' in html
    assert "No job (won't show in any job's updates)" in html


def test_interview_form_makes_you_pick_when_in_several_jobs(store):
    j1, j2, arjun = _arjun(store)
    html = _form(store, arjun)
    assert "Pick a job (needed for an interview)" in html and 'value="" selected' in html
    assert "No job (won't show in any job's updates)" in html
    c = local_client(store)
    data = {"kind": "interview", "body": "Onsite: coding", "at": "2030-01-10T10:00", "job_id": ""}
    c.post(f"/candidates/{arjun['id']}/log", data=data)
    assert not [t for t in store.timeline(arjun["id"]) if t["kind"] == "interview"]
    c.post(f"/candidates/{arjun['id']}/log", data={**data, "job_id": "none"})
    assert [t for t in store.timeline(arjun["id"]) if t["kind"] == "interview"][0]["job_id"] is None


def test_interview_posted_with_no_job_for_a_one_job_person_lands_on_that_job(store):
    job = store.add_job("Backend", "Acme")
    arjun = store.add_candidate(name="Arjun Rao")
    store.add_to_pipeline(arjun["id"], job["id"], "Screening")
    local_client(store).post(f"/candidates/{arjun['id']}/log", data={
        "kind": "interview", "body": "Screen", "at": "2030-01-10T10:00", "job_id": ""})
    assert [t for t in store.timeline(arjun["id"]) if t["kind"] == "interview"][0]["job_id"] == job["id"]
