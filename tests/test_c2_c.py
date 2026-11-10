"""Cycle 2, fixer C: import, export and paste."""
import csv
import io
import re

from conftest import local_client
from talent import cli, importers
from talent.store import Store

LINKEDIN_EXPORT = (
    "Notes:\n"
    '"When exporting your connection data, you may notice that some of the email addresses are missing. '
    'You will only see email addresses for connections who have allowed it: https://www.linkedin.com/psettings/x"\n'
    "\n"
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Maya,Okafor,https://www.linkedin.com/in/maya-okafor,,Ledgerline,Staff Engineer,05 Nov 2026\n"
    "Ben,Cole,https://www.linkedin.com/in/bencole,ben@x.com,Initech,PM,01 Oct 2026\n"
)


def _run(store, text, mapping=None, **kw):
    header, rows = importers.read_csv(text)
    return importers.import_rows(store, header, rows, mapping or importers.guess_mapping(header, rows),
                                 backup=False, **kw)


# ------------------------------------------------------------ 1. LinkedIn
def test_linkedin_connections_csv_in_one_step(store):
    header, rows = importers.read_csv(LINKEDIN_EXPORT)
    assert header[:3] == ["First Name", "Last Name", "URL"] and len(rows) == 2
    mapping = importers.guess_mapping(header, rows)
    assert mapping == ["first_name", "last_name", "linkedin_url", "email", "current_company", "current_title", "note"]
    assert importers.default_source(header) == "LinkedIn connections"
    r = importers.import_rows(store, header, rows, mapping, source="LinkedIn connections", backup=False)
    assert r["added"] == 2 and r["not_imported"] == []  # Connected On becomes a dated timeline note
    maya = store.find_by_name("Maya Okafor")[0]
    assert maya["linkedin_url"] == "https://linkedin.com/in/maya-okafor"
    assert (maya["current_title"], maya["current_company"], maya["source"]) == (
        "Staff Engineer", "Ledgerline", "LinkedIn connections")


def test_url_column_without_linkedin_links_stays_website():
    header, rows = importers.read_csv("Name,URL\nAna,https://ana.dev\n")
    assert importers.guess_mapping(header, rows) == ["name", "website"]


def test_import_page_handles_linkedin_export(store):
    c = local_client(store)
    r = c.post("/import", files={"file": ("Connections.csv", LINKEDIN_EXPORT.encode())})
    assert "Pick the column" not in r.text and 'value="LinkedIn connections"' in r.text
    assert "Will add 2, update 0" in r.text and store.count_candidates() == 0


# ------------------------------------------------------- 2. guess_mapping
def test_job_title_beats_role_and_where_we_met_is_source():
    assert importers.guess_mapping(["Name", "Role", "Job Title"]) == ["name", "", "current_title"]
    assert importers.guess_mapping(["Name", "Where we met", "How we met", "Met at"]) == [
        "name", "source", "", ""]
    assert importers.guess_mapping(["Name", "City", "State"]) == ["name", "location", "location"]
    assert importers.guess_mapping(["Name", "Current Role"]) == ["name", "current_title"]
    # A bare Role on a sheet with no client or status is still their title.
    assert importers.guess_mapping(["Name", "Role"]) == ["name", "current_title"]


def test_role_at_company_location_parts_and_labeled_notes(store):
    text = ("Name,Current Role,City,State,Salary Expectation,Notice Period\n"
            "Alex Rivera,Frontend Lead at Ramp,Austin,TX,$210k,2 weeks\n")
    header, rows = importers.read_csv(text)
    mapping = importers.guess_mapping(header, rows)
    assert mapping == ["name", "current_title", "location", "location", "note", "note"]
    _run(store, text)
    alex = store.find_by_name("Alex Rivera")[0]
    assert (alex["current_title"], alex["current_company"], alex["location"]) == ("Frontend Lead", "Ramp", "Austin, TX")
    notes = [t["body"] for t in store.timeline(alex["id"]) if t["kind"] == "note"]
    assert sorted(notes) == ["Notice Period: 2 weeks", "Salary Expectation: $210k"]


# ------------------------------------------------------------ 3. dry run
def test_preview_counts_without_writing(store):
    maya = store.add_candidate(name="Maya Okafor", email="maya@x.com", current_title="Engineer",
                               current_company="Ledgerline")
    store.add_candidate(name="Sam Okoro")
    store.log(maya["id"], "note", "first call")
    text = ("Name,Email,Title,Company\nMaya Okafor,maya@x.com,Staff Engineer,Ledgerline\n"
            "Maria Gonzalez,maya@x.com,CEO,OtherCo\nNew Person,new@x.com,Dev,\nSam Okoro,,PM,Acme\n")
    header, rows = importers.read_csv(text)
    before = importers.export_csv(store)
    p = importers.preview_import(store, header, rows, importers.guess_mapping(header, rows))
    assert importers.export_csv(store) == before and store.count_candidates() == 2
    assert (p["added"], p["updated"], p["conflicts"], len(p["possible_duplicates"])) == (2, 1, 1, 1)
    lines = importers.preview_lines(p)
    assert lines[0] == ("Will add 2, update 1 (title), leave 1 alone because of conflicts, flag 1 possible "
                        "duplicate (no email or LinkedIn, and the name matches someone here).")
    assert p["dry_run"] and p["backup"] is None


def test_cli_dry_run_and_backup_path(store, tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.db"
    monkeypatch.setattr(cli, "db_path", lambda: db)
    Store(db).close()
    f = tmp_path / "in.csv"
    f.write_text("Name,Email,Notes\nAna,ana@x.com,hello\n")
    cli.main(["import-csv", str(f), "--dry-run"])
    out = capsys.readouterr().out
    assert "Will add 1, update 0" in out and "Nothing was changed (dry run)." in out
    assert Store(db).count_candidates() == 0
    cli.main(["import-csv", str(f)])
    out = capsys.readouterr().out
    assert "1 note added" in out and "Backup from before this import: " in out and ".db" in out


def test_check_again_reworks_the_preview(store):
    store.add_candidate(name="Ana", email="ana@x.com", current_title="Dev")
    c = local_client(store)
    r = c.post("/import", files={"file": ("p.csv", b"Name,Email,Title\nAna,ana@x.com,Lead\n")})
    token = re.search(r'name="token" value="(\w+)"', r.text).group(1)
    assert "Will add 0, update 1 (title)" in r.text
    r = c.post("/import/check", data={"token": token, "col0": "name", "col1": "email", "col2": "",
                                      "source": "Old sheet"})
    assert "Will add 0, update 0" in r.text and 'value="Old sheet"' in r.text
    assert store.find_duplicate("ana@x.com")["current_title"] == "Dev"


# --------------------------------------------------------- 4. pipelines
AGENCY = ("Name,Email,Role,Client,Status,Job Title\n"
          "Priya Raman,priya@x.com,Senior Backend Engineer,Acme,screen,Engineer\n"
          "Tom Nguyen,tom@x.com,Senior Backend Engineer,Acme,Offer,Engineer\n"
          "Lee Chen,lee@x.com,Designer,,Phone call maybe,Designer\n"
          "Bo Park,bo@x.com,Senior Backend Engineer,Acme,Placed,Engineer\n")


def test_role_client_status_build_pipelines(store):
    existing = store.add_job("Senior Backend Engineer", "Acme")
    header, rows = importers.read_csv(AGENCY)
    mapping = importers.guess_mapping(header, rows)
    assert mapping == ["name", "email", "job_title", "job_company", "stage", "current_title"]
    r = importers.import_rows(store, header, rows, mapping, backup=False)
    assert r["pipelines_added"] == 4 and r["jobs_created"] == ["Designer"]
    stages = {p["candidate_name"]: p["stage"] for p in store.pipeline(existing["id"])}
    assert stages == {"Priya Raman": "Screening", "Tom Nguyen": "Offer", "Bo Park": "Hired"}
    designer = [j for j in store.list_jobs() if j["title"] == "Designer"][0]
    assert [p["stage"] for p in store.pipeline(designer["id"])] == ["Sourced"]
    assert "Row 4: put in Sourced, since Designer has no stage called 'Phone call maybe'" in r["stage_fallbacks"]
    # Importing again adds nobody twice and makes no second job.
    r = importers.import_rows(store, header, rows, mapping, backup=False)
    assert r["pipelines_added"] == 0 and r["jobs_created"] == []


def test_status_without_a_job_is_left_out(store):
    r = _run(store, "Name,Status\nAna,Interviewing\n", ["name", "stage"])
    assert r["warnings"] == ["Row 2: left the client and stage out, there's no job title on this row"]
    assert importers.guess_mapping(["Name", "Status"]) == ["name", "tag_column"]  # kept as a tag (cycle 11 T)


# --------------------------------------------------------- 5. duplicates
def test_same_email_rows_collapse_before_writing(store):
    text = ("Name,Email,Title,Location,Notes\nJordan Pierce,j@x.com,Senior Engineer,San Francisco,first\n"
            "Jordan Pierce,J@x.com,Staff Engineer,,second\n")
    for _ in range(2):
        r = _run(store, text)
    assert (r["added"], r["updated"], r["unchanged"]) == (0, 0, 1)
    j = store.find_duplicate("j@x.com")
    assert (j["current_title"], j["location"]) == ("Staff Engineer", "San Francisco")
    assert sorted(t["body"] for t in store.timeline(j["id"]) if t["kind"] == "note") == ["first", "second"]


def test_no_email_rows_match_by_name_and_company_or_flag(store):
    text = "Name,Company,Phone\nJosé Álvarez,Acme,\nSam Okoro,,415 555 0100\nLee Chen,,\n"
    store.add_candidate(name="Jose Alvarez", current_company="ACME")
    store.add_candidate(name="Sam Okoro", phone="(415) 555-0100", email="sam@x.com")
    store.add_candidate(name="Lee Chen")
    r = _run(store, text)
    assert (r["added"], r["updated"], r["unchanged"]) == (1, 1, 1)  # ACME -> Acme, Sam unchanged
    assert len(r["possible_duplicates"]) == 1 and r["possible_duplicates"][0].startswith("Row 4: Lee Chen was added")
    r = _run(store, text)
    assert store.count_candidates() == 5 and "(id 3, id 4)" in r["possible_duplicates"][0]  # nothing to tell apart


# ----------------------------------------------------- 6. import details
def test_relative_dates_formulas_and_dated_notes(store):
    text = ("Name,Email,Last Spoke,Notes,Title\nAna,ana@x.com,yesterday,call went well,=HYPERLINK(\"x\")\n"
            "Bo,bo@x.com,3/5/2026,liked the role,Dev\n")
    r = _run(store, text)
    assert "Row 2: left Last Spoke blank, 'yesterday' depends on when the sheet was written." in " ".join(r["warnings"])
    ana, bo = store.find_duplicate("ana@x.com"), store.find_duplicate("bo@x.com")
    assert ana["last_contacted_at"] is None and ana["current_title"] == '=HYPERLINK("x")'  # text is kept as written (cycle 13)
    note = [t for t in store.timeline(bo["id"]) if t["kind"] == "note"][0]
    assert note["at"] == bo["last_contacted_at"]


def test_source_name_on_changes_listed_changes_and_tag_case(store):
    store.add_candidate(name="Ana", email="ana@x.com", current_title="Dev", tags="Python")
    r = _run(store, "Name,Email,Title,Skills,Owner\nAna,ana@x.com,Lead,python\nBo,bo@x.com,PM,PYTHON; dbt\n",
             source="Old agency sheet")
    assert r["changes"] == ["Row 2 (Ana): title"] and r["not_imported"] == ["Owner"]
    change = [t for t in store.timeline(store.find_duplicate("ana@x.com")["id"]) if t["kind"] == "change"][0]
    assert change["source"] == "Old agency sheet"
    assert store.find_duplicate("bo@x.com")["tags"] == ["Python", "dbt"]


def test_done_page_lists_changes_and_skipped_columns(store):
    store.add_candidate(name="Ana", email="ana@x.com", current_title="Dev")
    c = local_client(store)
    r = c.post("/import", files={"file": ("p.csv", b"Name,Email,Title,Owner\nAna,ana@x.com,Lead,Me\n")})
    token = re.search(r'name="token" value="(\w+)"', r.text).group(1)
    r = c.post("/import/run", data={"token": token, "col0": "name", "col1": "email", "col2": "current_title",
                                    "col3": "", "source": "Old sheet"})
    assert "Row 2 (Ana): title" in r.text and "Not imported: Owner." in r.text


# ------------------------------------------------------- 7. round trip
def test_export_has_jobs_and_notes_and_round_trips(store, tmp_path):
    ana = store.add_candidate(name="Ana Diaz", email="ana@x.com")
    job = store.add_job("Senior Backend Engineer", "Acme")
    store.add_to_pipeline(ana["id"], job["id"], stage="Screening")
    store.log(ana["id"], "note", "older | with a pipe", at="2026-01-01")
    store.log(ana["id"], "note", "newer", at="2026-02-01")
    text = importers.export_csv(store)
    out = list(csv.DictReader(io.StringIO(text)))
    assert out[0]["Jobs"] == "Senior Backend Engineer (Acme): Screening"
    assert out[0]["Notes"] == "newer | older \\| with a pipe"
    assert list(out[0])[-1] == "Work history"

    r = _run(store, text)  # back into the same database: nothing doubles
    assert (r["notes"], r["pipelines_added"], r["jobs_created"]) == (0, 0, [])

    fresh = Store(tmp_path / "fresh.db")
    r = _run(fresh, text)
    a = fresh.find_duplicate("ana@x.com")
    assert [p["stage"] for p in fresh.candidate_pipelines(a["id"])] == ["Screening"]
    assert [t["body"] for t in fresh.timeline(a["id"]) if t["kind"] == "note"] == ["newer", "older | with a pipe"]
    assert r["jobs_created"] == ["Senior Backend Engineer (Acme)"]
    fresh.close()


# --------------------------------------------------------------- 8. paste
RESUME = """Sam Rivera
Senior Backend Engineer | Austin, TX
sam@example.com | (512) 555-0100 | linkedin.com/in/samrivera

Summary
Backend engineer who likes payments.

Experience
Senior Backend Engineer at Stripe (2021 - Present)
Built ledgers.
Backend Engineer | Square | Jan 2018 - Mar 2021

Education
UT Austin, 2017

Skills
Go, Python, PostgreSQL; gRPC
"""


def test_paste_resume_reads_location_skills_and_experience():
    got = importers.quick_parse(RESUME)
    assert got["source"] == "Resume" and got["location"] == "Austin, TX"
    assert got["current_title"] == "Senior Backend Engineer"
    assert got["tags"] == ["Go", "Python", "PostgreSQL", "gRPC"]
    assert got["work_history"] == [
        {"title": "Senior Backend Engineer", "company": "Stripe", "start": "2021", "end": "now"},
        {"title": "Backend Engineer", "company": "Square", "start": "Jan 2018", "end": "Mar 2021"}]
    assert got["about"] == "Backend engineer who likes payments."


def test_paste_linkedin_experience_and_page_fills_history(store):
    text = ("Jane Doe\n· 2nd\nStaff Engineer at Foo Corp\n500+ connections\nExperience\nStaff Engineer\n"
            "Foo Corp · Full-time\nJan 2021 - Present · 3 yrs\n")
    got = importers.quick_parse(text)
    assert got["source"] == "LinkedIn"
    assert got["work_history"] == [{"title": "Staff Engineer", "company": "Foo Corp", "start": "Jan 2021", "end": "now"}]
    r = local_client(store).post("/paste", data={"text": RESUME})
    assert "Senior Backend Engineer | Stripe | 2021 | now" in r.text and 'value="Resume"' in r.text
    assert 'value="Go, Python, PostgreSQL, gRPC"' in r.text
