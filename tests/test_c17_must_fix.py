"""Cycle 17 must-fix bugs: a share file for a job holds only that job's people (no rejected or withdrawn ones, no
one who isn't in it), email and phone written in About never reach a file or page without contact details, and an
import's stand-in 1 opening never makes a job "filled" or a client update say "44 hired against 1 opening"."""
from __future__ import annotations

from conftest import local_client
from test_c13_c import read_xlsx

from talent import actions, importers, reports, xlsx_export
from talent.mcp_server import build_server

HEADER = "Name,Email,Phone,LinkedIn,Title,Job,Client,Stage,Stage Entered\n"


def _names(book):
    rows = book["Candidates"]["rows"]
    at = next(i for i, r in enumerate(rows) if r and r[0] == "Name")  # after the note line, when there is one
    return [r[0] for r in rows[at + 1:]]


def _about_sheet(book):
    sheet = next(v for k, v in book.items() if k != "Candidates")
    return {r[0]: r[1] for r in sheet["rows"] if len(r) > 1}


def _job_with_exits(store):
    job = store.add_job("Platform Engineer", "Acme", stages=["Sourced", "Interview", "Hired", "Rejected", "Withdrawn"])
    other = store.add_job("Designer", "Acme")
    ana = store.add_candidate(name="Ana In Play", email="ana@x.com")
    rex = store.add_candidate(name="Rex Rejected", email="rex@x.com")
    wil = store.add_candidate(name="Wil Withdrawn", email="wil@x.com")
    out = store.add_candidate(name="Otto Elsewhere", email="otto@x.com")
    store.add_to_pipeline(ana["id"], job["id"], "Interview")
    store.add_to_pipeline(rex["id"], job["id"], "Rejected")
    store.add_to_pipeline(wil["id"], job["id"], "Withdrawn")
    store.add_to_pipeline(out["id"], other["id"], "Rejected")  # rejected in another job, not in this one
    return job, [ana, rex, wil, out]


# ---------------------------------------------------------------- 1. the picked job's people only
def test_share_with_a_job_lists_only_its_people_in_play(store):
    job, people = _job_with_exits(store)
    ids = ",".join(str(p["id"]) for p in people)
    res = local_client(store).post("/candidates/share", data={"ids": ids, "job_id": str(job["id"])})
    book = read_xlsx(res.content)
    assert _names(book) == ["Ana In Play"]
    left = _about_sheet(book).get("People left out", "")
    assert "2 people rejected or withdrawn in this job" in left and "1 person not in this job" in left


def test_share_with_a_job_and_rejected_ticked_still_leaves_out_other_jobs_people(store):
    job, people = _job_with_exits(store)
    ids = ",".join(str(p["id"]) for p in people)
    res = local_client(store).post("/candidates/share", data={"ids": ids, "job_id": str(job["id"]),
                                                              "include_rejected": "1"})
    assert sorted(_names(read_xlsx(res.content))) == ["Ana In Play", "Rex Rejected", "Wil Withdrawn"]


def test_share_with_a_job_nobody_is_in_says_so_instead_of_an_empty_file(store):
    job, people = _job_with_exits(store)
    res = local_client(store).post("/candidates/share", data={"ids": str(people[3]["id"]), "job_id": str(job["id"])})
    assert res.headers["content-type"].startswith("text/html")
    assert "Nobody here is still in Platform Engineer" in res.text


def test_share_preview_counts_say_why_people_are_left_out(store):
    job, people = _job_with_exits(store)
    pv = xlsx_export.share_preview(store, [p["id"] for p in people], hl=[])["jobs"][str(job["id"])]["out"]
    assert pv["count"] == 1 and pv["not_in_job"] == 1 and pv["rejected_out"] == 2
    html = local_client(store).get("/candidates/share?ids=" + ",".join(str(p["id"]) for p in people)
                                   + f"&job={job['id']}").text
    assert "left out: 1 not in this job, 2 rejected or withdrawn" in html


def test_chat_export_for_a_job_leaves_out_exits_and_says_so(store, tmp_path, monkeypatch):
    monkeypatch.setattr(xlsx_export, "save_to_downloads", lambda data, name: tmp_path / name)
    job, people = _job_with_exits(store)
    tools = build_server(store)._tool_manager._tools
    res = tools["export_candidates_xlsx"].fn(candidate_ids=[p["id"] for p in people], job_id=job["id"])
    assert res.structuredContent["people"] == 1
    assert "Left out 3 people not in Platform Engineer" in res.content[0].text


# ---------------------------------------------------------------- 2. contact details written in About
def _with_contact_in_about(store):
    return store.add_candidate(
        name="Dana About", email="dana@x.com", phone="415-555-0100",
        about="Great engineer. Reach me at dana.private@gmail.com or (415) 555-0134. Led a team of 12 from 2019-2023.",
        work_history=[{"title": "Engineer", "company": "Beta (call +44 20 7946 0958)", "start": "2019", "end": "2023"}])


def test_share_file_without_contact_takes_emails_and_phones_out_of_about(store):
    dana = _with_contact_in_about(store)
    book = read_xlsx(local_client(store).post("/candidates/share", data={"ids": str(dana["id"])}).content)
    text = repr(book["Candidates"]["rows"])
    assert "dana.private@gmail.com" not in text and "555-0134" not in text and "7946" not in text
    assert "(email removed)" in text and "(phone removed)" in text
    assert "Led a team of 12 from 2019-2023" in text  # numbers that are not phone numbers stay
    assert "3 email addresses, phone numbers or profile links taken out" in _about_sheet(book)["Contact details"]


def test_share_file_with_contact_ticked_keeps_about_as_written(store):
    dana = _with_contact_in_about(store)
    book = read_xlsx(local_client(store).post("/candidates/share", data={"ids": str(dana["id"]),
                                                                        "include_contact": "1"}).content)
    assert "dana.private@gmail.com" in repr(book["Candidates"]["rows"])


def test_chat_export_has_no_email_or_phone_anywhere_as_it_claims(store, tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(xlsx_export, "save_to_downloads",
                        lambda data, name: saved.setdefault("data", data) and tmp_path / name)
    dana = _with_contact_in_about(store)
    res = build_server(store)._tool_manager._tools["export_candidates_xlsx"].fn(candidate_ids=[dana["id"]])
    assert "no email, phone or notes" in res.content[0].text
    text = repr(read_xlsx(saved["data"]))
    assert "dana.private@gmail.com" not in text and "555-0134" not in text and "dana@x.com" not in text


def test_share_preview_shows_about_and_work_history_as_the_file_will(store):
    dana = _with_contact_in_about(store)
    html = local_client(store).get(f"/candidates/share?ids={dana['id']}").text
    assert "About as it will appear:" in html and "Work history:" in html
    assert "Reach me at (email removed) or (phone removed)." in html
    assert "3 contact details (email, phone or profile link) taken out." in html


def test_hiring_manager_compare_view_takes_contact_out_of_about(store):
    job = store.add_job("Platform Engineer")
    dana = _with_contact_in_about(store)
    eli = store.add_candidate(name="Eli Other", about="Plain about.")
    for c in (dana, eli):
        store.add_to_pipeline(c["id"], job["id"], "Sourced")
    html = local_client(store).get(f"/candidates/compare?ids={dana['id']},{eli['id']}&view=hm").text
    assert "dana.private@gmail.com" not in html and "555-0134" not in html and "(email removed)" in html


# ---------------------------------------------------------------- 3. import-made jobs and their openings
def _import_hires(store, hired=44, active=3):
    rows = [f"H{i},h{i}@x.com,,,Eng,Platform,Initech,Hired,10/01/2026\n" for i in range(hired)]
    rows += [f"A{i},a{i}@x.com,,,Eng,Platform,Initech,Interview,10/01/2026\n" for i in range(active)]
    header, data = importers.read_csv(HEADER + "".join(rows))
    importers.import_rows(store, header, data, importers.guess_mapping(header, data), backup=False,
                          add_stages=True, move_stages=True)
    return store.list_jobs()[0]


def test_import_made_job_is_not_called_filled_and_offers_no_close_out(store):
    job = _import_hires(store)
    nums = actions.job_numbers(store, job["id"])
    assert nums["unset"] and not nums["filled"]
    assert actions.close_out_plan(store, job["id"]) is None
    html = local_client(store).get(f"/jobs/{job['id']}").text
    assert "This role is filled" not in html and "Move the other" not in html


def test_status_updates_never_say_hired_against_the_stand_in_opening(store):
    job = _import_hires(store)
    digest = actions.status_digest(store)
    assert "against 1 opening" not in digest and "of 1 opening filled" not in digest
    assert "44 hired so far" in digest
    assert "against 1 opening" not in actions.job_status_text(store, job["id"])
    text = build_server(store)._tool_manager._tools["status_digest"].fn().content[0].text
    assert "against 1 opening" not in text


def test_skipping_the_openings_question_still_leaves_the_job_unset(store):
    job = _import_hires(store)
    importers.answer_openings(store, {job["id"]: None})  # "don't ask again"
    assert job["id"] not in importers.openings_to_ask(store)
    assert not actions.job_numbers(store, job["id"])["filled"]


def test_setting_openings_makes_the_job_count_against_them(store):
    job = _import_hires(store)
    c = local_client(store)
    form = c.get(f"/jobs/{job['id']}/edit").text
    assert 'placeholder="Not set yet"' in form
    c.post(f"/jobs/{job['id']}", data={"title": job["title"], "openings": ""})  # saved blank: still unset
    assert job["id"] in store.openings_unset_ids()
    c.post(f"/jobs/{job['id']}", data={"title": job["title"], "openings": "50"})
    assert job["id"] not in store.openings_unset_ids() and job["id"] not in importers.openings_to_ask(store)
    nums = actions.job_numbers(store, job["id"])
    assert nums["openings"] == 50 and not nums["unset"] and not nums["filled"]
    store.update_job(job["id"], openings=40)
    assert actions.job_numbers(store, job["id"])["filled"]


def test_a_job_made_by_hand_still_defaults_to_one_opening(store):
    job = store.add_job("Hand Made")
    store.add_to_pipeline(store.add_candidate(name="Hy")["id"], job["id"], "Hired")
    assert actions.job_numbers(store, job["id"])["filled"]
