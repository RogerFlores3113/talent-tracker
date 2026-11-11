"""Cycle 19 must-fix bugs: submit_to_client refuses someone not in the job (nothing moves or is added), an interview
with no job never shows in client-facing text, chat log_activity defaults an interview to the person's one job,
strip_contact catches phones, spelled-out emails and profile links without eating years, the write-up lists only what
is there, and the log form's error is announced and keeps what was typed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import local_client

from talent import chase, reports, xlsx_export
from talent.mcp_server import build_server
from talent.xlsx_export import strip_contact

STAGES = ["Sourced", "Screening", "Submitted to client", "Interview", "Hired", "Rejected", "Withdrawn"]


def _tool(store, name):
    return build_server(store)._tool_manager._tools[name].fn


def _soon():
    return (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()


# ------------------------------------------------------------------ A. submit refuses someone not in the job
def test_mcp_submit_refuses_a_person_not_in_the_job_and_changes_nothing(store):
    job = store.add_job("Platform Engineer", "Acme", stages=STAGES)
    outsider = store.add_candidate(name="Otto Outside")
    with pytest.raises(Exception, match="isn't in Platform Engineer"):
        _tool(store, "submit_to_client")(candidate_id=outsider["id"], job_id=job["id"])
    assert store.candidate_pipelines(outsider["id"]) == []


def test_chase_submit_checks_out_of_job_before_any_move(store):
    job = store.add_job("Platform Engineer", "Acme", stages=STAGES)
    rex = store.add_candidate(name="Rex Rejected")
    out = store.add_candidate(name="Otto Outside")
    store.add_to_pipeline(rex["id"], job["id"], "Rejected")
    for who, word in ((rex, "rejected"), (out, "isn't in")):
        with pytest.raises(ValueError, match=word):
            chase.submit(store, who["id"], job["id"])
    assert store.candidate_pipelines(out["id"]) == []
    assert store.candidate_pipelines(rex["id"])[0]["stage"] == "Rejected"
    with pytest.raises(ValueError, match="isn't in"):
        chase.writeup(store, out["id"], job["id"])


def test_mcp_submit_says_what_is_included_and_left_out(store):
    job = store.add_job("Platform Engineer", "Acme", stages=STAGES)
    ana = store.add_candidate(name="Ana", about="Builds things.", email="ana@x.com", tags=["go"])
    bare = store.add_candidate(name="Bo")
    store.add_to_pipeline(ana["id"], job["id"], "Screening")
    store.add_to_pipeline(bare["id"], job["id"], "Screening")
    text = _tool(store, "submit_to_client")(candidate_id=ana["id"], job_id=job["id"]).content[0].text
    assert "Included: " in text and "About" in text and "Left out: email, phone and profile link" in text
    assert "private notes" in text
    plain = _tool(store, "submit_to_client")(candidate_id=bare["id"], job_id=job["id"]).content[0].text
    assert "About" not in plain.split("Included:")[1].split("Left out:")[0]


# ------------------------------------------------------------------ B. interviews with no job stay out of client text
def _one_job_person(store):
    job = store.add_job("Product Designer", "Acme", stages=["Sourced", "Screening", "Interview", "Hired", "Rejected"])
    jules = store.add_candidate(name="Jules Ferrand")
    store.add_to_pipeline(jules["id"], job["id"], "Screening")
    return job, jules


def test_interview_with_no_job_is_not_in_the_client_report_text_or_desk(store):
    job, jules = _one_job_person(store)
    store.log(jules["id"], "interview", "Values chat", at=_soon(), job_id=None)
    rep = reports.report(store, "acme")
    assert rep["upcoming"] == []
    text = reports.report_text(rep)
    assert "Values chat" not in text and "Upcoming interviews" not in text
    assert [r for r in reports.overview(store) if r["key"] == "acme"][0]["interviews"] == 0
    tool = _tool(store, "client_report")(client="acme")
    assert "Values chat" not in tool.content[0].text and tool.structuredContent["upcoming_interviews"] == []
    page = local_client(store).get("/clients/acme").text
    assert "Values chat" not in page
    assert "Jules Ferrand's interview" in " ".join(reports.recruiter_notes(rep))  # only the recruiter's notes say so


def test_interview_with_a_job_still_shows_in_that_clients_report(store):
    job, jules = _one_job_person(store)
    store.log(jules["id"], "interview", "Values chat", at=_soon(), job_id=job["id"])
    rep = reports.report(store, "acme")
    assert "Jules Ferrand" in reports.report_text(rep)


# ------------------------------------------------------------------ C. chat log_activity defaults the job
def test_log_activity_defaults_an_interview_to_the_persons_one_job(store):
    job, jules = _one_job_person(store)
    res = _tool(store, "log_activity")(candidate_id=jules["id"], kind="interview", summary="Values chat",
                                       when="2030-01-10T10:00:00-08:00")
    assert [t for t in store.timeline(jules["id"]) if t["kind"] == "interview"][0]["job_id"] == job["id"]
    assert "the only job" in res.content[0].text


def test_log_activity_with_several_jobs_stays_jobless_and_says_so(store):
    j1 = store.add_job("Backend", "Acme")
    j2 = store.add_job("Data Lead", "Acme")
    arjun = store.add_candidate(name="Arjun Rao")
    store.add_to_pipeline(arjun["id"], j1["id"], "Screening")
    store.add_to_pipeline(arjun["id"], j2["id"], "Sourced")
    res = _tool(store, "log_activity")(candidate_id=arjun["id"], kind="interview", summary="Onsite",
                                       when="2030-01-10T10:00:00-08:00")
    assert [t for t in store.timeline(arjun["id"]) if t["kind"] == "interview"][0]["job_id"] is None
    text = res.content[0].text + repr(res.structuredContent)
    assert "won't show in any job's client updates" in text and f"job id {j1['id']}" in text and "job_id" in text


# ------------------------------------------------------------------ D. strip_contact
@pytest.mark.parametrize("text", [
    "call 4155550100 now", "090-5555-0110", "020 7946 0958", "1-(415) 555-0134", "+44 20 7946 0958", "(415) 555-0134",
    "415.555.0134", "555-0134"])
def test_strip_contact_removes_phone_formats(text):
    out, n = strip_contact(text)
    assert n == 1 and "(phone removed)" in out and not any(ch.isdigit() for ch in out), out
    assert "1-(phone removed)" not in out


@pytest.mark.parametrize("text", ["ben [at] x.com", "ben (at) x.com", "ben at x dot com", "ben [at] x [dot] com",
                                  "ben@x.com"])
def test_strip_contact_removes_spelled_out_emails(text):
    out, n = strip_contact(text)
    assert out == "(email removed)" and n == 1


@pytest.mark.parametrize("text", [
    "Worked 2019 - 2023 at Acme", "2019-2023", "2015 - 2019 - 2021", "5 years of Go", "Jan 2019 - Mar 2023",
    "since 2026-11-05", "$1,200,000 revenue", "5000-7000 users", "worked at Acme and met ben", "1999 2001 2003"])
def test_strip_contact_keeps_years_ranges_and_short_numbers(text):
    assert strip_contact(text) == (text, 0)


def test_strip_contact_removes_profile_links():
    out, n = strip_contact("See https://example.com/ben, linkedin.com/in/ben and github.com/ben.")
    assert out == "See (link removed), (link removed) and (link removed)." and n == 3


def _contact_world(store):
    job = store.add_job("Platform Engineer", "Acme", stages=STAGES)
    ana = store.add_candidate(
        name="Ana", email="ana@x.com", phone="415-555-0100", linkedin_url="https://linkedin.com/in/ana-secret",
        about="Strong. 4155550100, ana [at] gmail.com, github.com/anasecret. At Beta 2019 - 2023.",
        work_history=[{"title": "Engineer", "company": "Beta (090-5555-0110, ben at x dot com)",
                       "start": "2019", "end": "2023"}])
    store.add_to_pipeline(ana["id"], job["id"], "Screening")
    return job, ana


def test_writeup_strips_all_contact_formats_and_the_profile_line(store):
    job, ana = _contact_world(store)
    body = chase.writeup(store, ana["id"], job["id"])["body"]
    for bad in ("4155550100", "gmail.com", "anasecret", "090-5555", "x dot com", "ana-secret", "Profile:", "ana@x.com"):
        assert bad not in body, bad
    assert "2019 - 2023" in body and "(phone removed)" in body and "(link removed)" in body
    withc = chase.writeup(store, ana["id"], job["id"], include_contact=True)["body"]
    assert "Profile: https://linkedin.com/in/ana-secret" in withc and "ana@x.com" in withc


def test_share_file_free_text_follows_the_rule_and_says_so(store):
    job, ana = _contact_world(store)
    t = xlsx_export.share_table(store, [ana["id"]])
    row = " ".join(str(v) for v in t["rows"][0])
    for bad in ("4155550100", "gmail.com", "anasecret", "090-5555", "x dot com"):
        assert bad not in row, bad
    assert "2019 - 2023" in row and t["contact_removed"] >= 4
    html = local_client(store).get(f"/candidates/share?ids={ana['id']}").text
    assert "phone numbers and profile links" in html
    assert "anasecret" not in html.split("data-full")[0].split("About as it will appear")[1].split("data-full")[0]


# ------------------------------------------------------------------ E. the page lists only what is there
def test_submit_page_lists_only_what_is_there_and_says_about_is_copied(store):
    job = store.add_job("Platform Engineer", "Acme", stages=STAGES)
    bare = store.add_candidate(name="Bo Bare")
    store.add_to_pipeline(bare["id"], job["id"], "Screening")
    c = local_client(store)
    html = c.get(f"/jobs/{job['id']}/submit/{bare['id']}?notes=1").text
    inc = html.split('id="writeup-included"')[1].split("</li>")[0]
    assert "About" not in inc and "newest notes" not in inc
    assert "copied as written, apart from contact details" in html
    store.update_candidate(bare["id"], about="Builds things.")
    store.log(bare["id"], "note", "Solid screen.", job_id=job["id"])
    inc = c.get(f"/jobs/{job['id']}/submit/{bare['id']}?notes=1").text.split('id="writeup-included"')[1].split("</li>")[0]
    assert "About" in inc and "newest notes" in inc


# ------------------------------------------------------------------ G. the log form's error is announced
def test_log_form_error_is_an_alert_and_keeps_what_was_typed(store):
    j1 = store.add_job("Backend", "Acme")
    j2 = store.add_job("Data Lead", "Acme")
    arjun = store.add_candidate(name="Arjun Rao")
    store.add_to_pipeline(arjun["id"], j1["id"], "Screening")
    store.add_to_pipeline(arjun["id"], j2["id"], "Sourced")
    res = local_client(store).post(f"/candidates/{arjun['id']}/log", data={
        "kind": "interview", "body": "Onsite: coding", "at": "2030-01-10T10:00", "tz": "America/Chicago",
        "job_id": ""})
    assert res.status_code == 400
    html = res.text
    assert html.count('role="alert"') >= 1 and 'id="log-error"' in html and "Pick which job" in html
    err = html.split('id="log-error"')[1].split("</p>")[0]
    assert "autofocus" in html.split('id="log-error"')[0][-80:] + err
    assert "aria-live" not in html.split('id="log-error"')[0][-200:]
    assert 'value="Onsite: coding"' in html and 'value="2030-01-10T10:00"' in html
    assert '<option value="interview" selected>' in html and 'value="America/Chicago"' in html
    assert not [t for t in store.timeline(arjun["id"]) if t["kind"] == "interview"]


# ------------------------------------------------------------------ F. skills and tags are one list
def test_share_and_writeup_say_skills_are_the_whole_tag_list(store):
    job, ana = _contact_world(store)
    store.update_candidate(ana["id"], tags="go, sourced-nov")
    c = local_client(store)
    assert "your whole tag list" in c.get(f"/candidates/share?ids={ana['id']}").text
    assert "whole tag list" in c.get(f"/jobs/{job['id']}/submit/{ana['id']}").text


def test_strip_contact_does_not_swallow_a_year_next_to_a_phone():
    assert strip_contact("Beta (call +44 20 7946 0958) (2019 to 2023)") == ("Beta (call (phone removed)) (2019 to 2023)", 1)
    assert strip_contact("(2019-2023) and (415) 555-0134.") == ("(2019-2023) and (phone removed).", 1)
