"""Cycle 5 fixer D: weekly ATS re-import that keeps history (sync moves, stage order, name-only rows, column
matches, Today, sync profiles)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from conftest import flash_of, local_client

from talent import actions, backups, importers, reports


def _read(text):
    header, rows = importers.read_csv(text)
    return header, rows


def _run(store, text, **kw):
    header, rows = _read(text)
    mapping = kw.pop("mapping", None) or importers.guess_mapping(header, rows)
    kw.setdefault("backup", False)
    kw.setdefault("add_stages", True)
    return importers.import_rows(store, header, rows, mapping, **kw)


def _token(r):
    return re.search(r'name="token" value="(\w+)"', r.text).group(1)


def _stage(store, job_id, name):
    return next(a["stage"] for a in store.pipeline(job_id) if a["candidate_name"] == name)


WEEK1 = ("Name,Email,Job,Client,Status\n"
         "Ana Diaz,ana@x.com,iOS Engineer,Initech,Recruiter Screen\n"
         "Bo Li,bo@x.com,iOS Engineer,Initech,Recruiter Screen\n"
         "Cy Ng,cy@x.com,iOS Engineer,Initech,Submitted to Client\n")


# --------------------------------------------------------------- 1. sync moves
def test_resync_moves_people_dated_from_stage_entered_and_counts_as_moves(store):
    _run(store, WEEK1)
    job = next(j for j in store.list_jobs() if j["title"] == "iOS Engineer")
    week2 = ("Name,Email,Job,Client,Status,Stage Entered\n"
             "Ana Diaz,ana@x.com,iOS Engineer,Initech,Submitted to Client,2026-11-02\n"
             "Bo Li,bo@x.com,iOS Engineer,Initech,Recruiter Screen,2026-10-20\n"
             "Cy Ng,cy@x.com,iOS Engineer,Initech,Client Interview,2026-11-03\n")
    p = importers.preview_import(store, *_read(week2), importers.guess_mapping(*_read(week2)), add_stages=True,
                                 move_stages=True)
    assert len(p["moved"]) == 2 and _stage(store, job["id"], "Ana Diaz") == "Recruiter Screen"  # a dry run
    assert importers.preview_lines(p)[0].startswith("Will add 0, update 0, move 2 people to the stage the file shows")
    r = _run(store, week2, move_stages=True)
    assert r["moved"] == ["Row 2 (Ana Diaz): Recruiter Screen to Submitted to Client on iOS Engineer (Initech)",
                          "Row 4 (Cy Ng): Submitted to Client to Client Interview on iOS Engineer (Initech)"]
    ana = store.find_duplicate("ana@x.com")
    app = next(a for a in store.pipeline(job["id"]) if a["candidate_id"] == ana["id"])
    assert app["stage"] == "Submitted to Client" and app["stage_changed_at"].startswith("2026-11-0")
    entry = next(t for t in store.timeline(ana["id"]) if t["kind"] == "stage" and t["body"] == "Submitted to Client")
    data = entry["data"] if isinstance(entry["data"], dict) else json.loads(entry["data"])
    assert entry["source"] == "ATS sync" and data["by"] == "sync" and data["pipeline"]["old"] == "Recruiter Screen"
    rep = reports.report(store, actions.company_key("Initech"), since="2026-10-01",
                         now=datetime(2026, 11, 5, tzinfo=timezone.utc))
    assert {m["name"] for m in rep["moves"]} == {"Ana Diaz", "Cy Ng"}  # real moves, not imports
    # The same file again changes nothing.
    again = _run(store, week2, move_stages=True)
    assert again["moved"] == [] and again["already_in"] == []


def test_a_move_made_in_the_app_is_kept_unless_the_file_is_newer(store):
    _run(store, WEEK1)
    job = next(j for j in store.list_jobs() if j["title"] == "iOS Engineer")
    bo = store.find_duplicate("bo@x.com")
    store.move_stage(bo["id"], job["id"], "Interviewing", by="app")
    r = _run(store, "Name,Email,Job,Client,Status\nBo Li,bo@x.com,iOS Engineer,Initech,Submitted to Client\n",
             move_stages=True)
    assert r["moved"] == [] and _stage(store, job["id"], "Bo Li") == "Interviewing"
    assert r["already_in"] == ["Row 2: already in iOS Engineer (Initech) at Interviewing (the file says Submitted to "
                               "Client) and moved in the app since, so they were left there"]
    later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    store._conn.execute("UPDATE applications SET stage_changed_at = '2026-01-01T00:00:00+00:00'")
    r = _run(store, f"Name,Email,Job,Client,Status,Stage Entered\nBo Li,bo@x.com,iOS Engineer,Initech,Offer,"
                    f"{later[:10]}\n", move_stages=True)
    assert len(r["moved"]) == 1 and _stage(store, job["id"], "Bo Li") == "Offer"


def test_without_move_stages_nobody_moves_and_the_file_stage_is_shown(store):
    _run(store, WEEK1)
    r = _run(store, "Name,Email,Job,Client,Status\nAna Diaz,ana@x.com,iOS Engineer,Initech,Offer\n")
    assert r["moved"] == []
    assert "(the file says Offer), so they were left there" in r["already_in"][0]


def test_people_missing_from_the_file_are_listed_not_touched(store):
    _run(store, WEEK1)
    r = _run(store, "Name,Email,Job,Client,Status\nAna Diaz,ana@x.com,iOS Engineer,Initech,Recruiter Screen\n",
             move_stages=True)
    assert len(r["missing_from_file"]) == 2 and "Bo Li is at Recruiter Screen on iOS Engineer (Initech)" in \
        r["missing_from_file"][0]
    assert store.find_duplicate("bo@x.com") and len(store.pipeline(r["job_keys"]["ios engineer|initech"])) == 3


# ------------------------------------------------------------- 2. stage order
def test_added_stages_go_in_by_meaning_with_one_order_for_every_job(store):
    text = ("Name,Email,Job,Client,Status\n"
            "A1,a1@x.com,iOS,Initech,Offer Extended\n"
            "A2,a2@x.com,iOS,Initech,Submitted to Client\n"
            "A3,a3@x.com,iOS,Initech,Recruiter Screen\n"
            "A4,a4@x.com,iOS,Initech,Not a Fit\n"
            "B1,b1@x.com,Android,Initech,Recruiter Screen\n"
            "B2,b2@x.com,Android,Initech,Reference Check\n"
            "B3,b3@x.com,Android,Initech,Offer Extended\n"
            "B4,b4@x.com,Android,Initech,Submitted to Client\n")
    _run(store, text)
    orders = []
    for title in ("iOS", "Android"):
        job = next(j for j in store.list_jobs() if j["title"] == title)
        names = [s["name"] for s in store.job_stages(job["id"])]
        orders.append([n for n in names if n in ("Recruiter Screen", "Submitted to Client", "Offer Extended")])
        assert names.index("Offer Extended") == names.index("Hired") - 1  # an offer last before the hire
        assert names.index("Screening") < names.index("Submitted to Client") < names.index("Interviewing")
        if title == "iOS":
            assert names[-1] == "Not a Fit"  # out stages at the end
        else:
            assert names.index("Interviewing") < names.index("Reference Check") < names.index("Offer Extended")
    assert orders[0] == orders[1] == ["Recruiter Screen", "Submitted to Client", "Offer Extended"]


def test_stage_rank_reads_meaning():
    r = importers.stage_rank
    assert (r("Sourced"), r("Phone Screen"), r("Client Review"), r("HM Screen"), r("Onsite"), r("Reference Check"),
            r("Offer"), r("Hired"), r("Rejected"), r("Stage 3")) == (0, 2, 3, 4, 5, 6, 7, 8, 9, None)


# --------------------------------------------------------- 3. name-only rows
def test_no_email_row_never_merges_into_a_namesake_by_company(store):
    hana = store.add_candidate(name="Hana Garcia", email="hana@x.com", current_company="Initech",
                               current_title="iOS Lead", location="Austin", phone="512 555 0101")
    text = "Name,Company,Title,Location,Phone\nHana García,Initech,QA Analyst,Denver,303 555 0199\n"
    r = _run(store, text)
    assert r["added"] == 1 and r["updated"] == 0 and "Hana Garc" in r["possible_duplicates"][0]
    assert store.get_candidate(hana["id"])["current_title"] == "iOS Lead"
    again = _run(store, text)  # the next sync finds the row's own record by its phone, and changes nothing
    assert (again["added"], again["updated"], again["unchanged"]) == (0, 0, 1)
    assert store.get_candidate(hana["id"])["current_title"] == "iOS Lead" and store.count_candidates() == 2


def test_no_email_row_with_no_phone_is_new_when_the_namesake_has_an_email(store):
    store.add_candidate(name="Hana Garcia", email="hana@x.com", current_company="Initech")
    r = _run(store, "Name,Company\nHana Garcia,Initech\n")
    assert r["added"] == 1 and len(r["possible_duplicates"]) == 1


# ------------------------------------------------------ 4. columns and maps
def test_stage_entered_created_date_and_recruiter_are_recognised():
    header, rows = _read("Name,Email,Job,Client,Status,Stage Entered,Created Date,Recruiter\n"
                         "Ana,ana@x.com,iOS,Initech,Offer,2026-11-01,2026-01-05,Marcus\n")
    assert importers.guess_mapping(header, rows)[5:] == ["stage_changed_at", "created_at", "tag_column"]


def test_mapping_is_remembered_per_header_set_on_the_web(store):
    c = local_client(store)
    text = b"Full Name,E-mail,Notes Field\nAna,ana@x.com,hello\n"
    r = c.post("/import", files={"file": ("a.csv", text)})
    r = c.post("/import/run", data={"token": _token(r), "filename": "a.csv", "col0": "name", "col1": "email",
                                    "col2": "about"})
    assert "Added 1" in r.text and "talent sync FILE" in r.text
    r = c.post("/import", files={"file": ("b.csv", b"full name,e-mail,notes field\nBo,bo@x.com,hi\n")})
    assert "the matches you used last time" in r.text
    assert re.search(r'name="col2"[^>]*>.*?<option value="about" selected', r.text, re.S)


def test_apply_map_and_its_errors():
    header = ["Name", "Stage Entered", "Owner"]
    out = importers.apply_map(header, ["name", "", ""], "stage entered=Date they reached that stage; Owner=tag_column")
    assert out == ["name", "stage_changed_at", "tag_column"]
    assert importers.apply_map(header, out, "Owner=skip") == ["name", "stage_changed_at", ""]
    for bad, says in (("Nope=email", "No column called"), ("Owner=wages", "isn't a field"), ("Owner", "Column=field")):
        try:
            importers.apply_map(header, out, bad)
        except ValueError as exc:
            assert says in str(exc) and "—" not in str(exc)
        else:
            raise AssertionError(bad)


def test_cli_import_map_option(store, tmp_path, monkeypatch):
    from talent import cli, paths
    from talent.store import Store

    f = tmp_path / "x.csv"
    f.write_text("Name,Email,Owner\nAna,ana@x.com,Marcus\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["talent", "import-csv", str(f), "--map", "Owner=tag_column"])
    cli.main()
    s = Store(paths.db_path())
    try:
        assert "Owner: Marcus" in s.find_duplicate("ana@x.com")["tags"]
        assert importers.sync_profile(s, ["Name", "Email", "Owner"])["mapping"][2] == "tag_column"
    finally:
        s.close()


# --------------------------------------------------------------- 5. Today
def test_today_starts_no_stuck_clock_from_imported_dates_and_groups_by_job(store):
    rows = "".join(f"P{i},p{i}@x.com,iOS,Initech,Screening,2026-01-0{1 + i % 9}\n" for i in range(6))
    _run(store, "Name,Email,Job,Client,Status,Stage Entered\n" + rows)
    assert not [i for i in actions.action_items(store)["items"] if i["kind"] == "stuck"]
    later = datetime.now(timezone.utc) + timedelta(days=10)
    stuck = [i for i in actions.action_items(store, now=later)["items"] if i["kind"] == "stuck"]
    assert len(stuck) == 1 and stuck[0]["count"] == 6 and stuck[0]["title"].startswith("6 people on iOS")
    run = stuck[0]["import_run"]
    assert run and actions.snooze_import(store, run) == 6
    assert not [i for i in actions.action_items(store, now=later)["items"] if i["kind"] == "stuck"]


def test_today_caps_stuck_rows(store):
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    for j in range(60):
        job = store.add_job(f"Job {j}")
        cid = store.add_candidate(name=f"P{j}", email=f"p{j}@x.com")["id"]
        store.add_to_pipeline(cid, job["id"], stage="Screening")
    store._conn.execute("UPDATE applications SET stage_changed_at = ?", (old,))
    res = actions.action_items(store)
    assert len([i for i in res["items"] if i["kind"] == "stuck"]) == actions.STUCK_CAP and res["stuck_more"] == 10


def test_today_never_offers_a_move_back_up_the_funnel(store):
    job = store.add_job("iOS", stages=["Sourced", "Submitted to Client", "Recruiter Screen", "Hired", "Rejected"])
    cid = store.add_candidate(name="Ana", email="a@x.com")["id"]
    store.add_to_pipeline(cid, job["id"], stage="Submitted to Client")
    store._conn.execute("UPDATE applications SET stage_changed_at = '2026-01-01T00:00:00+00:00'")
    item = next(i for i in actions.action_items(store)["items"] if i["kind"] == "stuck")
    assert item["next_stage"] is None


def test_snooze_import_route_and_openings_ask(store):
    c = local_client(store)
    text = ("Name,Email,Job,Client,Status\n" + "".join(f"P{i},p{i}@x.com,iOS,Initech,Hired\n" for i in range(7))
            + "Q,q@x.com,iOS,Initech,Screening\n").encode()
    r = c.post("/import", files={"file": ("ats.csv", text)})
    r = c.post("/import/run", data={"token": _token(r), "filename": "ats.csv", "col0": "name", "col1": "email",
                                    "col2": "job_title", "col3": "job_company", "col4": "stage", "add_stages": "1"})
    assert "How many openings does each new job have?" in r.text
    job = next(j for j in store.list_jobs() if j["title"] == "iOS")
    assert job["openings"] == 1  # the default, until asked: the hire count is never taken as the openings
    page = c.get("/").text
    assert "How many openings does iOS, Initech have?" in page and "Set openings" in page
    r = c.post("/import/openings", data={f"openings_{job['id']}": "8"}, follow_redirects=False)
    assert store.get_job(job["id"])["openings"] == 8 and "Saved openings for 1 job." in flash_of(r)
    assert importers.openings_to_ask(store) == [] and "Set openings" not in c.get("/").text
    assert "No jobs are waiting for their number of openings." in c.get("/import/openings").text
    r = c.post("/actions/snooze-import", data={"run": "nope"}, follow_redirects=False)
    assert "Nothing to snooze" in flash_of(r, "flash_error")


# ------------------------------------------------------------ 6. sync profiles
def test_web_reimport_shows_moves_with_a_tickbox_and_applies_as_one_change(store):
    c = local_client(store)
    cols = {"col0": "name", "col1": "email", "col2": "job_title", "col3": "job_company", "col4": "stage"}
    r = c.post("/import", files={"file": ("week1.csv", WEEK1.encode())})
    c.post("/import/run", data={"token": _token(r), "filename": "week1.csv", "add_stages": "1", **cols})
    week2 = WEEK1.replace("Bo Li,bo@x.com,iOS Engineer,Initech,Recruiter Screen",
                          "Bo Li,bo@x.com,iOS Engineer,Initech,Submitted to Client")
    r = c.post("/import", files={"file": ("week2.csv", week2.encode())})
    assert "move 1 person to the stage the file shows" in r.text
    assert re.search(r'name="move_stages" value="1"\s+checked', r.text) and "(1 will move)" in r.text
    assert "Stage moves from the file (1)" in r.text
    r = c.post("/import/run", data={"token": _token(r), "filename": "week2.csv", "add_stages": "1",
                                    "move_stages": "1", **cols})
    assert "Moved 1 person to the stage the file shows" in r.text
    job = next(j for j in store.list_jobs() if j["title"] == "iOS Engineer")
    assert _stage(store, job["id"], "Bo Li") == "Submitted to Client"
    log = backups.import_log(store)
    assert backups.import_text(log[0]) == "Imported week2.csv: 3 rows, 1 stage move"
    assert "Undo this import" in c.get("/changes").text
    prof = importers.sync_profile(store, importers.read_csv(week2)[0])
    assert prof["runs"] == 2 and prof["move_stages"] and prof["jobs"]["ios engineer|initech"] == job["id"]
    assert prof["stage_order"][:2] == ["Recruiter Screen", "Submitted to Client"]


def test_profile_job_match_survives_a_rename_and_client_fills_in(store):
    text = "Name,Email,Job,Status\nAna,ana@x.com,iOS,Screening\n"
    header, rows = _read(text)
    mapping = importers.guess_mapping(header, rows)
    r = importers.import_rows(store, header, rows, mapping, backup=False, profile={"client": "Initech"})
    job = store.get_job(r["new_job_ids"][0])
    assert job["company"] == "Initech"
    importers.save_sync_profile(store, header, mapping, r, client="Initech")
    store.update_job(job["id"], title="iOS Engineer (Senior)")
    r = importers.import_rows(store, header, rows + [["Bo", "bo@x.com", "iOS", "Screening"]], mapping, backup=False,
                              profile=importers.sync_profile(store, header))
    assert r["jobs_created"] == [] and len(store.pipeline(job["id"])) == 2


def test_talent_sync_shows_the_diff_and_applies(store, tmp_path, monkeypatch, capsys):
    from talent import cli, paths
    from talent.store import Store

    f = tmp_path / "ats.csv"
    f.write_text(WEEK1, encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["talent", "sync", str(f), "--yes"])
    cli.main()
    f.write_text(WEEK1.replace("Cy Ng,cy@x.com,iOS Engineer,Initech,Submitted to Client",
                               "Cy Ng,cy@x.com,iOS Engineer,Initech,Offer"), encoding="utf-8")
    capsys.readouterr()
    monkeypatch.setattr("sys.argv", ["talent", "sync", str(f), "--dry-run"])
    cli.main()
    out = capsys.readouterr().out
    assert "Using the saved matches for this export (ats.csv)." in out
    assert "Stage moves (1):" in out and "Submitted to Client to Offer on iOS Engineer (Initech)" in out
    assert "Nothing was changed (dry run)." in out
    monkeypatch.setattr("builtins.input", lambda _: "n")
    monkeypatch.setattr("sys.argv", ["talent", "sync", str(f)])
    cli.main()
    assert "Nothing was changed." in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda _: "y")
    cli.main()
    out = capsys.readouterr().out
    assert "moved 1" in out and "one row on Changes" in out
    s = Store(paths.db_path())
    try:
        job = next(j for j in s.list_jobs() if j["title"] == "iOS Engineer")
        assert _stage(s, job["id"], "Cy Ng") == "Offer"
        assert "1 stage move" in backups.import_text(backups.import_log(s)[0])
    finally:
        s.close()


def test_new_copy_has_no_dashes(store):
    lines = importers.diff_lines({"added": 1, "updated": 0, "unchanged": 0, "skipped_rows": 0, "moved": ["Row 2 (A): x"],
                                  "missing_from_file": ["A is at X on Y here, but isn't in the file"]})
    item = actions._openings_item(store)
    assert item is None
    for text in lines:
        assert "—" not in text and "–" not in text
