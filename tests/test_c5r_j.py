"""Cycle 5 re-review, fixer J: sync is idempotent and knows closed jobs; date-only dates; import tidy-ups."""
import re
from datetime import datetime, timedelta

import pytest

from talent import actions, importers, reports
from talent.mcp_server import build_server
from talent.store import STAGES, to_dt

from conftest import local_client
from test_mcp import call

HEADER = "Name,Email,Phone,LinkedIn,Title,Job,Client,Stage,Stage Entered\n"


def _run(store, text, **kw):
    header, rows = importers.read_csv(text)
    mapping = importers.guess_mapping(header, rows)
    return importers.import_rows(store, header, rows, mapping, backup=False, add_stages=True, move_stages=True, **kw)


def _dry(store, text, **kw):
    header, rows = importers.read_csv(text)
    mapping = importers.guess_mapping(header, rows)
    return importers.preview_import(store, header, rows, mapping, add_stages=True, move_stages=True, **kw)


def _token(r):
    return re.search(r'name="token" value="(\w+)"', r.text).group(1)


WEEK1 = HEADER + (
    "Ana Ruiz,ana@x.com,,,Engineer,iOS Engineer,Initech,Sourced,10/01/2026\n"
    "Ben Ode,ben@x.com,,,Engineer,iOS Engineer,Initech,Submitted to Client,10/02/2026\n"
    "Cy Lee,cy@x.com,,,Designer,Data Engineer,Acme,Recruiter Screen,10/03/2026\n")


# ------------------------------------------------------------- 1. closed jobs
def test_a_job_closed_here_is_never_made_again_and_its_rows_are_left_alone(store):
    _run(store, WEEK1)
    ios = next(j for j in store.list_jobs() if j["title"] == "iOS Engineer")
    store.update_job(ios["id"], status="closed")
    moved = WEEK1.replace("Sourced,10/01/2026", "Submitted to Client,10/20/2026") + \
        "Dee Kay,dee@x.com,,,Engineer,iOS Engineer,Initech,Sourced,10/21/2026\n"
    r = _dry(store, moved)
    assert r["jobs_created"] == [] and r["moved"] == [] and r["pipelines_added"] == 0
    assert r["closed_jobs"] == [{"job": "iOS Engineer (Initech)", "status": "closed", "rows": 3}]
    assert "iOS Engineer (Initech) is closed here: leaving its 3 rows alone." in importers.preview_lines(r)
    r = _run(store, moved)
    assert len([j for j in store.list_jobs() if j["title"] == "iOS Engineer"]) == 1
    assert store.get_job(ios["id"])["status"] == "closed" and r["jobs_created"] == []
    ana = store.find_duplicate(email="ana@x.com")
    assert next(p for p in store.candidate_pipelines(ana["id"]) if p["job_id"] == ios["id"])["stage"] == "Sourced"


def test_reopen_opts_in_to_syncing_a_closed_job(store):
    _run(store, WEEK1)
    ios = next(j for j in store.list_jobs() if j["title"] == "iOS Engineer")
    store.update_job(ios["id"], status="on_hold")
    moved = WEEK1.replace("Sourced,10/01/2026", "Submitted to Client,10/20/2026")
    assert "is on hold here: leaving its 2 rows alone." in importers.preview_lines(_dry(store, moved))[-1]
    r = _dry(store, moved, reopen_jobs=True)
    assert r["reopened"] == ["iOS Engineer (Initech)"] and len(r["moved"]) == 1 and not r["closed_jobs"]
    assert store.get_job(ios["id"])["status"] == "on_hold"  # a dry run changes nothing
    r = _run(store, moved, reopen_jobs=True)
    assert store.get_job(ios["id"])["status"] == "open" and r["jobs_created"] == []


def test_web_sync_offers_reopen_for_a_closed_job(store):
    c = local_client(store)
    cols = {f"col{i}": m for i, m in enumerate(["name", "email", "phone", "linkedin_url", "current_title",
                                                 "job_title", "job_company", "stage", "stage_changed_at"])}
    r = c.post("/import", files={"file": ("ats.csv", WEEK1.encode())})
    c.post("/import/run", data={"token": _token(r), "filename": "ats.csv", "add_stages": "1", "move_stages": "1",
                                **cols})
    ios = next(j for j in store.list_jobs() if j["title"] == "iOS Engineer")
    store.update_job(ios["id"], status="closed")
    r = c.post("/import", files={"file": ("ats.csv", WEEK1.encode())})
    assert "iOS Engineer (Initech) is closed here: leaving its 2 rows alone." in r.text
    assert 'name="reopen_jobs"' in r.text and "new job" not in r.text
    r = c.post("/import/run", data={"token": _token(r), "filename": "ats.csv", "add_stages": "1",
                                    "move_stages": "1", **cols})
    assert "is closed here: leaving its 2 rows alone." in r.text
    assert len([j for j in store.list_jobs() if j["title"] == "iOS Engineer"]) == 1


# ------------------------------------------------------------- 2. idempotent sync
TWICE = HEADER + (
    "Ana Ruiz,ana@x.com,,,Engineer,iOS Engineer,Initech,Submitted to Client,10/09/2026\n"
    "Ana Ruiz,ana@x.com,,,Senior Engineer,iOS Engineer,Initech,Sourced,10/01/2026\n"
    "Hana Cruz,,(415) 555-0170,,Manager,Data Engineer,Acme,Applied,09/28/2026\n"
    "Hana Cruz,hana@x.com,(415) 555-0170,linkedin.com/in/hana-cruz,Senior Engineer,iOS Engineer,Initech,"
    "Submitted to Client,08/28/2026\n"
    "Bo Li,,415-555-0199,,Analyst,Data Engineer,Acme,Applied,09/01/2026\n"
    "Bo Li,,(415) 555 0199,,Analyst,Data Engineer,Acme,Recruiter Screen,09/05/2026\n")


def test_resyncing_an_unchanged_file_has_nothing_to_do(store):
    r = _run(store, TWICE)
    ana = store.find_duplicate(email="ana@x.com")
    ios = next(j for j in store.list_jobs() if j["title"] == "iOS Engineer")
    stage = next(p for p in store.candidate_pipelines(ana["id"]) if p["job_id"] == ios["id"])["stage"]
    assert stage == "Submitted to Client"  # the row with the latest stage date, not the last row
    assert any("Only this row (Submitted to Client) counts, it has the latest stage date" in m
               for m in r["merged_rows"])
    assert len([c for c in store.all_candidates() if c["name"] == "Hana Cruz"]) == 1  # same name and phone
    assert len([c for c in store.all_candidates() if c["name"] == "Bo Li"]) == 1
    again = _dry(store, TWICE)
    assert again["moved"] == [] and again["changes"] == [] and again["updated"] == 0 and again["added"] == 0
    assert again["pipelines_added"] == 0 and again["missing_from_file"] == []
    assert importers.preview_lines(again)[0] == "Nothing will change: 3 people are in this file already up to date."
    again = _run(store, TWICE)
    assert again["backup"] is None  # nothing changed


def test_a_later_row_on_a_job_with_no_dates_wins(store):
    text = HEADER.replace(",Stage Entered", "") + ("Ana Ruiz,ana@x.com,,,Eng,iOS,Initech,Sourced\n"
                                                    "Ana Ruiz,ana@x.com,,,Eng,iOS,Initech,Offer\n")
    _run(store, text)
    assert store.pipeline(store.list_jobs()[0]["id"])[0]["stage"] == "Offer"
    assert _dry(store, text)["moved"] == []


# ------------------------------------------------------------- 3. date-only dates
def _skip_far(zone):
    here = datetime(2026, 10, 3, 12).astimezone().utcoffset().total_seconds() / 3600
    there = datetime(2026, 10, 3, 12, tzinfo=reports._zone(zone)).utcoffset().total_seconds() / 3600
    if abs(here) > 11 and abs(here - there) > 12:
        pytest.skip("this computer is far east or west of UTC: its own day comes first")


@pytest.mark.parametrize("zone", ["America/Los_Angeles", "America/Chicago", "America/New_York", "Europe/London",
                                  "Asia/Kolkata", "Asia/Tokyo", "Pacific/Honolulu", "UTC"])
def test_a_date_only_stage_date_reads_as_that_day_in_every_zone(store, zone):
    _run(store, HEADER + "Ana Ruiz,ana@x.com,,,Eng,iOS,Initech,Sourced,10/03/2026\n")
    app = store.pipeline(store.list_jobs()[0]["id"])[0]
    _skip_far(zone)
    assert reports.in_zone(app["stage_changed_at"], zone, "%Y-%m-%d") == "2026-10-03"
    assert reports.in_zone(app["stage_changed_at"], None, "%Y-%m-%d") == "2026-10-03"  # this computer's zone


def test_a_date_with_a_time_keeps_its_time_and_today_is_not_the_future(store):
    today = datetime.now().strftime("%m/%d/%Y")
    _run(store, HEADER + "Ana Ruiz,ana@x.com,,,Eng,iOS,Initech,Sourced,2026-10-03T08:15:00Z\n"
         + f"Ben Ode,ben@x.com,,,Eng,iOS,Initech,Offer,{today}\n")
    by = {a["candidate_name"]: a for a in store.pipeline(store.list_jobs()[0]["id"])}
    assert by["Ana Ruiz"]["stage_changed_at"] == "2026-10-03T08:15:00+00:00"
    when = to_dt(by["Ben Ode"]["stage_changed_at"])
    assert when.astimezone().strftime("%m/%d/%Y") == today
    assert when <= datetime.now().astimezone() + timedelta(seconds=1)


def test_client_zone_update_shows_the_files_day(store):
    _run(store, HEADER + "Ana Ruiz,ana@x.com,,,Eng,iOS,Initech,Sourced,11/03/2026\n")
    app = store.pipeline(store.list_jobs()[0]["id"])[0]
    for zone in ("America/Chicago", "America/New_York", "America/Los_Angeles"):
        _skip_far(zone)
        assert reports.in_zone(app["stage_changed_at"], zone, "%a %b %d") == "Tue Nov 03"


# ------------------------------------------------------------- 4. openings, close-out, stages, Changes
def test_openings_are_not_filled_in_from_the_hire_count(store):
    _run(store, HEADER + "".join(f"P{i},p{i}@x.com,,,Eng,iOS,Initech,Hired,10/01/2026\n" for i in range(5)))
    job = store.list_jobs()[0]
    assert job["openings"] == 1 and job["id"] in importers.openings_to_ask(store)


def test_close_out_uses_the_jobs_own_rejection_stage(store):
    job = store.add_job("iOS", stages=["Sourced", "Hired", "Rejected", "Withdrawn"])
    store.add_stage(job["id"], "Rejected by Client")
    store.add_stage(job["id"], "Candidate Withdrew")
    for i, st in enumerate(["Rejected by Client", "Candidate Withdrew", "Candidate Withdrew"]):
        store.add_to_pipeline(store.add_candidate(name=f"P{i}", email=f"p{i}@x.com")["id"], job["id"], stage=st)
    assert actions.out_stage_name(store, job["id"]) == "Rejected by Client"
    plain = store.add_job("Backend")
    assert actions.out_stage_name(store, plain["id"]) == "Rejected"


def test_replace_default_stages_leaves_only_the_files_stages(store):
    text = HEADER + ("Ana Ruiz,ana@x.com,,,Eng,iOS,Initech,Applied,10/01/2026\n"
                     "Ben Ode,ben@x.com,,,Eng,iOS,Initech,Client Review,10/02/2026\n"
                     "Cy Lee,cy@x.com,,,Eng,iOS,Initech,Rejected by Client,10/03/2026\n")
    r = _run(store, text, replace_stages=True)
    names = [s["name"] for s in store.job_stages(store.list_jobs()[0]["id"])]
    assert names == ["Applied", "Client Review", "Hired", "Rejected by Client"]  # Hired stays: the file has none
    assert r["defaults_dropped"] == 7
    assert "7 empty default stages left off the new jobs" in importers.preview_lines(r)[1]
    other = _run(store, text.replace("iOS", "SRE"))
    sre = next(j for j in store.list_jobs() if j["title"] == "SRE")
    assert set(STAGES) <= {s["name"] for s in store.job_stages(sre["id"])}  # without it, the defaults stay
    assert other["defaults_dropped"] == 0


def test_web_import_offers_replace_default_stages_ticked(store):
    c = local_client(store)
    r = c.post("/import", files={"file": ("ats.csv", WEEK1.encode())})
    assert re.search(r'name="replace_stages" value="1" checked', r.text)
    assert "Replace the default stages" in r.text


def test_changes_shows_one_row_per_import_with_its_people_under_it(store):
    c = local_client(store)
    cols = {f"col{i}": m for i, m in enumerate(["name", "email", "phone", "linkedin_url", "current_title",
                                                 "job_title", "job_company", "stage", "stage_changed_at"])}
    for text in (WEEK1, WEEK1.replace("Engineer,iOS", "Lead,iOS")):
        r = c.post("/import", files={"file": ("ats.csv", text.encode())})
        c.post("/import/run", data={"token": _token(r), "filename": "ats.csv", "add_stages": "1",
                                    "move_stages": "1", **cols})
    page = c.get("/changes").text
    assert "2 people with changed details" in page
    top = page.split('<ul class="timeline changes">', 1)[1]
    rows = re.findall(r"^  <li>", top, flags=re.M)
    assert len(rows) == 2  # the two imports, with the per-person edits tucked under the second


# ------------------------------------------------------------- 5. MCP
def test_unknown_argument_on_a_tool_with_none_says_so(store):
    with pytest.raises(Exception) as exc:
        call(build_server(store), "list_action_items", bogus=1)
    msg = str(exc.value)
    assert "doesn't take bogus" in msg and "It takes no arguments" in msg and "accepts: ." not in msg
