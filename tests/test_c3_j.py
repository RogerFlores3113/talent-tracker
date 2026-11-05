"""Cycle 3, fixer J: plain-language AI replies, identity matching, paste and import mapping."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from conftest import local_client

from talent import importers
from talent.mcp_server import build_server
from talent.mcp_views import view_html
from talent.store import parse_day_text, parse_when, same_person_name, to_dt
from talent.web import change_lines
from test_mcp import call, structured, text


def _soon(days: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


# ------------------------------------------------------- 1. plain replies
def test_write_tools_lead_with_a_plain_sentence(store):
    server = build_server(store)
    job = structured(call(server, "create_job", title="Staff Backend Engineer", company="Acme Fintech"))
    out = call(server, "create_job", title="Designer")
    assert text(out).startswith("Created Designer (job id ") and "stages: Sourced, Contacted" in text(out)
    cid = store.add_candidate(name="Priya Raman")["id"]

    res = call(server, "add_to_pipeline", candidate_id=cid, job_id=job["id"])
    assert text(res).splitlines()[0] == "Added Priya Raman to Staff Backend Engineer (Acme Fintech), in Sourced."
    assert structured(res)["stage"] == "Sourced"  # structured data as before
    again = call(server, "add_to_pipeline", candidate_id=cid, job_id=job["id"])
    assert text(again).startswith("Priya Raman is already in Staff Backend Engineer (Acme Fintech), in Sourced.")

    moved = call(server, "move_stage", candidate_id=cid, job_id=job["id"], stage="screen")
    assert text(moved).splitlines()[0] == ("Moved Priya Raman to Screening on Staff Backend Engineer (Acme Fintech) "
                                           "(was Sourced).")
    upd = call(server, "update_job", job_id=job["id"], status="on hold", openings=2)
    assert text(upd).splitlines()[0] == "Updated Staff Backend Engineer (Acme Fintech): status on hold, openings 2."
    assert structured(upd)["openings"] == 2


def test_log_activity_says_when_in_local_time(store):
    server = build_server(store)
    cid = store.add_candidate(name="Dana Lee")["id"]
    at = _soon(3)
    res = call(server, "log_activity", candidate_id=cid, kind="interview", summary="Onsite", when=at)
    local = to_dt(at).astimezone()
    first = text(res).splitlines()[0]
    assert first.startswith(f"Logged an interview with Dana Lee for {local:%a %b} {local.day}, ")
    assert first.endswith(f"{local.tzname()}.")
    assert structured(res)["logged"]["at_local"] in first
    past = call(server, "log_activity", candidate_id=cid, kind="call", when="2026-01-05 1:00 PM")
    assert text(past).startswith("Logged a call with Dana Lee on Mon Jan 5, 1:00 PM")


def test_get_candidate_shows_local_times_and_the_client(store):
    server = build_server(store)
    cid = store.add_candidate(name="Priya Raman")["id"]
    job = store.add_job("Staff Backend Engineer", "Acme Fintech")
    store.add_to_pipeline(cid, job["id"])
    iv = store.log(cid, "interview", "Panel", at=_soon(2), job_id=job["id"])
    res = call(server, "get_candidate", candidate_id=cid)
    body = text(res)
    assert "Staff Backend Engineer (Acme Fintech): Sourced" in body
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", body)  # no raw UTC stamps
    assert f"interview (Staff Backend Engineer (Acme Fintech)) (interview id {iv['id']}, scheduled): Panel" in body
    data = structured(res)
    assert data["pipelines"][0]["job_company"] == "Acme Fintech"
    assert data["timeline"][0]["job_company"] == "Acme Fintech"
    assert "job_company" in view_html("candidate")


def test_date_errors_give_a_time_example_and_read_it():
    with pytest.raises(ValueError, match=r"2026-11-09 1:00 PM"):
        parse_when("next tuesday 2pm")
    local = to_dt(parse_when("2026-11-09 1:00 PM")).astimezone()
    assert (local.day, local.hour, local.minute) == (9, 13, 0)


# -------------------------------------------- 2/3. conflict and same name
def test_conflict_reply_points_to_the_app_and_renders(store):
    server = build_server(store)
    ben = store.add_candidate(name="Ben Cole", email="ben@x.com")
    out = call(server, "save_candidate", name="Ana Ruiz", email="ben@x.com")
    data = structured(out)
    assert data["conflict"] and data["existing"]["app_url"].endswith(f"/candidates/{ben['id']}")
    assert "ask the recruiter to change the name in the app" in data["warning"]
    assert "update_candidate" not in data["warning"]
    html = view_html("candidate")
    assert "notSaved" in html and "Already saved" in html


def test_same_name_lists_the_differing_fields(store):
    server = build_server(store)
    store.add_candidate(name="Priya Raman", current_title="Staff Engineer", current_company="Stripe")
    out = call(server, "save_candidate", name="Priya Raman", current_title="Staff Software Engineer",
               current_company="stripe")
    data = structured(out)
    assert data["needs_confirmation"]
    diff = data["possible_duplicates"][0]["differences"]
    assert diff == [{"field": "title", "saved": "Staff Engineer", "yours": "Staff Software Engineer"}]
    assert "title: saved 'Staff Engineer', yours 'Staff Software Engineer'" in text(out)


# ------------------------------------------------------------ 4. nicknames
def test_nicknames_are_the_same_first_name():
    assert same_person_name("Mike Okonkwo", "Michael Okonkwo")
    assert same_person_name("Liz Chen", "Elizabeth Chen") and same_person_name("Bob Stone", "Robert Stone")
    assert same_person_name("Bill Park", "William Park") and same_person_name("Jim Ito", "James Ito")
    assert not same_person_name("Mike Okonkwo", "Michael Smith")  # last names must match
    assert not same_person_name("Mike", "Michael Okonkwo")  # a nickname alone is not enough
    assert not same_person_name("Bob Stone", "William Stone")


def test_nickname_finds_and_flags_duplicates(store):
    mike = store.add_candidate(name="Michael Okonkwo", current_company="Mercury", email="m@x.com")
    other = store.add_candidate(name="Mike Okonkwo", current_company="Mercury")
    assert [c["id"] for c in store.find_by_name("Mike Okonkwo")] == [other["id"], mike["id"]]
    assert [d["id"] for d in store.possible_duplicates(other["id"])] == [mike["id"]]
    elsewhere = store.add_candidate(name="Mike Okonkwo", current_company="Ramp", email="mo@ramp.com")
    assert elsewhere["id"] not in [d["id"] for d in store.possible_duplicates(mike["id"])]
    # An import row with the nickname and the same email merges instead of being skipped.
    header, rows = importers.read_csv("Name,Email,Title\nMike Okonkwo,m@x.com,Staff Engineer\n")
    r = importers.import_rows(store, header, rows, ["name", "email", "current_title"], backup=False)
    assert r["skipped"] == [] and r["updated"] == 1


# --------------------------------------------------------- 5. web Add form
def test_web_add_form_checks_for_the_same_name(store):
    c = local_client(store)
    priya = store.add_candidate(name="Priya Raman", email="priya@gmail.com")
    job = store.add_job("Staff Backend Engineer", "Acme")
    form = {"name": "Priya Raman", "email": "priya@acme.com", "job_id": str(job["id"])}
    r = c.post("/candidates", data=form, follow_redirects=False)
    assert r.status_code == 200 and store.count_candidates() == 1
    assert f'href="/candidates/{priya["id"]}">Open Priya Raman</a>' in r.text
    assert f'action="/jobs/{job["id"]}/add"' in r.text and "Add them to this job" in r.text
    assert 'name="create_anyway" value="1"' in r.text
    r = c.post("/candidates", data={**form, "create_anyway": "1"}, follow_redirects=False)
    assert r.status_code == 303 and store.count_candidates() == 2
    # Same email is still a plain merge, no question asked.
    r = c.post("/candidates", data={"name": "Priya Raman", "email": "priya@gmail.com"}, follow_redirects=False)
    assert r.status_code == 303 and store.count_candidates() == 2


# ---------------------------------------------------------------- 6. paste
PASTE = """Jordan Okafor
He/Him
Founding Engineer @ Vercel alum | Building infra for AI apps
Brooklyn, New York, United States  Contact info
500+ connections

About
I like small teams and hard infra problems.

Experience
Staff Software Engineer
Vercel · Full-time
Jan 2022 - Present · 2 yrs 11 mos
Founding Engineer
Hightouch
2019 - 2021 · 2 yrs
"""


def test_paste_takes_the_present_role_and_the_contact_info_line():
    got = importers.quick_parse(PASTE)
    assert (got["current_title"], got["current_company"]) == ("Staff Software Engineer", "Vercel")
    assert got["location"] == "Brooklyn, New York, United States"


# --------------------------------------------------------------- 7. import
AGENCY = ("Name,Email,LinkedIn,Role,Client,Status\n"
          "Priya Raman,priya@x.com,,Staff Backend Engineer,Acme,Submitted\n"
          "Marcus Lee,marcus@x.com,,Staff Backend Engineer,Acme,Phone screen\n"
          "Dana Okafor,dana@x.com,,Staff Backend Engineer,Acme,Client Interview\n"
          "Priya Raman,priya@x.com,,Platform Lead,Brightline,Sourced\n")


def test_import_maps_close_stages_and_offers_missing_ones(store):
    header, rows = importers.read_csv(AGENCY)
    mapping = importers.guess_mapping(header, rows)
    p = importers.preview_import(store, header, rows, mapping)
    # Cycle 4: the tickbox adds every Status the job has no stage for, near ones too.
    assert p["missing_stages"] == [{"job": "Staff Backend Engineer (Acme)", "stage": s}
                                   for s in ("Submitted", "Phone screen", "Client Interview")]
    assert "Row 2: will be put in Sourced, since Staff Backend Engineer (Acme) has no stage called 'Submitted'" \
        in p["stage_fallbacks"]
    assert "Row 3: will be put in Screening, the closest stage to 'Phone screen' on Staff Backend Engineer (Acme)" \
        in p["stage_fallbacks"]
    assert not any(line.startswith("Row 5") for line in p["stage_fallbacks"])  # the Sourced row is fine
    assert store.list_jobs() == []  # a dry run changes nothing

    r = importers.import_rows(store, header, rows, mapping, backup=False, add_stages=True)
    job = next(j for j in store.list_jobs() if j["title"] == "Staff Backend Engineer")
    names = [s["name"] for s in store.job_stages(job["id"])]
    assert names.index("Submitted") < names.index("Hired")
    stages = {a["candidate_name"]: a["stage"] for a in store.pipeline(job["id"])}
    # With the box ticked, near stages become their own instead of merging (cycle 4).
    assert stages == {"Priya Raman": "Submitted", "Marcus Lee": "Phone screen", "Dana Okafor": "Client Interview"}
    assert r["merged_rows"] == ["Rows 2 and 5 are the same person (Priya Raman), so they were saved as one"]


def test_import_page_shows_the_stage_checkbox_and_heading(store):
    c = local_client(store)
    r = c.post("/import", files={"file": ("a.csv", AGENCY.encode())})
    assert 'name="add_stages" value="1" checked' in r.text and "Submitted on Staff Backend Engineer (Acme)" in r.text
    token = re.search(r'name="token" value="(\w+)"', r.text).group(1)
    cols = {f"col{i}": m for i, m in enumerate(["name", "email", "linkedin_url", "job_title", "job_company", "stage"])}
    r = c.post("/import/run", data={"token": token, **cols})  # box left unticked
    assert "Put in a different stage (3)" in r.text and "Rows saved as one person (1)" in r.text


def test_merged_rows_say_which_row_won(store):
    header, rows = importers.read_csv("Name,Email,Title\nAna,ana@x.com,Engineer\nAna,ana@x.com,Staff Engineer\n")
    r = importers.import_rows(store, header, rows, ["name", "email", "current_title"], backup=False)
    assert r["merged_rows"] == ["Rows 2 and 3 are the same person (Ana), so they were saved as one. "
                                "Where they differ, row 3 won (title)"]


def test_connected_on_becomes_a_dated_note(store):
    text_ = ("First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
             "Maya,Okafor,https://www.linkedin.com/in/maya-okafor,,Ledgerline,Staff Engineer,12 Mar 2023\n")
    header, rows = importers.read_csv(text_)
    r = importers.import_rows(store, header, rows, importers.guess_mapping(header, rows), backup=False)
    assert r["notes"] == 1
    maya = store.find_by_name("Maya Okafor")[0]
    note = store.timeline(maya["id"])[0]
    assert note["body"] == "Connected On: 12 Mar 2023" and to_dt(note["at"]).astimezone().date().isoformat() == "2023-03-12"


def test_email_and_linkedin_of_two_people_is_a_conflict(store):
    store.add_candidate(name="Ana Diaz", email="ana@x.com")
    store.add_candidate(name="Ana Diaz", linkedin_url="https://www.linkedin.com/in/anadiaz")
    header, rows = importers.read_csv("Name,Email,LinkedIn\nAna Diaz,ana@x.com,linkedin.com/in/anadiaz\n")
    r = importers.import_rows(store, header, rows, ["name", "email", "linkedin_url"], backup=False)
    assert r["conflicts"] == 1 and r["updated"] == r["unchanged"] == 0
    assert "the email is Ana Diaz's, id 1, and the LinkedIn is Ana Diaz's, id 2" in r["skipped"][0]


# ------------------------------------------------- 8. renames and roles
def test_timeline_follows_a_stage_rename(store):
    server = build_server(store)
    cid = store.add_candidate(name="Priya Raman")["id"]
    job = store.add_job("SRE", "Ramp")
    store.add_to_pipeline(cid, job["id"])
    store.move_stage(cid, job["id"], "Interviewing", by="ai")
    store.rename_stage(job["id"], "Interviewing", "Onsite")
    assert [t["body"] for t in store.timeline(cid) if t["kind"] == "stage"][0] == "Onsite"
    assert "moved to Onsite (SRE (Ramp))" in text(call(server, "get_candidate", candidate_id=cid))
    entry = next(t for t in store.timeline(cid) if t["undoable"])
    store.undo_change(entry["id"], candidate_id=cid)  # the undo still finds the stage it came from
    assert store.candidate_pipelines(cid)[0]["stage"] == "Sourced"


def test_changes_say_which_role_changed():
    data = {"old": {"work_history": [{"title": "Senior Engineer", "company": "Acme", "start": "2020", "end": None},
                                     {"title": "Dev", "company": "X", "start": "2018", "end": "2020"}]},
            "new": {"work_history": [{"title": "Staff Engineer", "company": "Acme", "start": "2020", "end": None},
                                     {"title": "Dev", "company": "X", "start": "2017", "end": "2020"},
                                     {"title": "Intern", "company": "Y", "start": "2016", "end": "2017"}]}}
    assert change_lines(data) == ["Work history: Dev at X now 2017 to 2020 (was 2018 to 2020); Senior Engineer at "
                                  "Acme to Staff Engineer at Acme; added Intern at Y"]


# ------------------------------------------------------- 9. years, tags
def test_dates_without_a_year_are_the_next_one_coming():
    today = datetime.now().date()
    later, earlier = today + timedelta(days=10), today - timedelta(days=10)
    assert parse_day_text(f"{later:%b} {later.day}") == later.isoformat()
    want = earlier.replace(year=earlier.year + 1) if not (earlier.month == 2 and earlier.day == 29) else None
    if want:
        assert parse_day_text(f"{earlier.day} {earlier:%B}") == want.isoformat()
        assert parse_day_text(f"{earlier.month}/{earlier.day}") == want.isoformat()


def test_tags_given_as_a_list_keep_their_commas(store):
    server = build_server(store)
    out = structured(call(server, "save_candidate", name="Bo", email="bo@x.com", tags=["B2B, enterprise sales", "Go"]))
    assert out["candidate"]["tags"] == ["B2B, enterprise sales", "Go"]
    one = store.add_candidate(name="Cy", tags=["Sales, SMB"])
    assert store.get_candidate(one["id"])["tags"] == ["Sales, SMB"]
    merged = structured(call(server, "save_candidate", name="Bo", email="bo@x.com", tags=["Rust"]))
    assert merged["candidate"]["tags"] == ["B2B, enterprise sales", "Go", "Rust"]
    assert store.add_candidate(name="Di", tags="python, sql")["tags"] == ["python", "sql"]  # typed text still splits
