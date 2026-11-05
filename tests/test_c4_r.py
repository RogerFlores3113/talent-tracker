"""Cycle 4 fixer R: import at scale (stages, dates, Changes rows, result panels, tag columns, big files)."""
from __future__ import annotations

import re

from conftest import local_client

from talent import actions, backups, importers
from talent.paths import backups_dir


def _run(store, text, add_stages=True, **kw):
    header, rows = importers.read_csv(text)
    mapping = importers.guess_mapping(header, rows)
    return importers.import_rows(store, header, rows, mapping, add_stages=add_stages, **kw), mapping


def _stages(store, job_id):
    return [s["name"] for s in store.job_stages(job_id)]


def _token(r):
    return re.search(r'name="token" value="(\w+)"', r.text).group(1)


def _cols(mapping):
    return {f"col{i}": m for i, m in enumerate(mapping)}


# ---------------------------------------------------------- 1. own stages
def test_ticked_box_keeps_near_stages_apart(store):
    job = store.add_job("Engineer", "Acme")
    text = ("Name,Email,Job,Client,Status\n"
            "Ana,ana@x.com,Engineer,Acme,HM Screen\n"
            "Bo,bo@x.com,Engineer,Acme,Phone Screen\n"
            "Cy,cy@x.com,Engineer,Acme,screening\n")
    r, _ = _run(store, text, backup=False)
    names = _stages(store, job["id"])
    assert "HM Screen" in names and "Phone Screen" in names
    assert names.index("Phone Screen") < names.index("Hired") and names.index("HM Screen") < names.index("Hired")
    got = {a["candidate_name"]: a["stage"] for a in store.pipeline(job["id"])}
    assert got == {"Ana": "HM Screen", "Bo": "Phone Screen", "Cy": "Screening"}  # exact match ignores case
    assert r["stage_fallbacks"] == []


def test_unticked_box_still_uses_the_closest_stage_and_counts_it(store):
    job = store.add_job("Engineer", "Acme")
    text = "Name,Email,Job,Client,Status\n" + "".join(
        f"P{i},p{i}@x.com,Engineer,Acme,Phone Screen\n" for i in range(25))
    r, _ = _run(store, text, add_stages=False, backup=False)
    assert {a["stage"] for a in store.pipeline(job["id"])} == {"Screening"}
    assert r["stage_moves"] == {"Phone Screen to Screening on Engineer (Acme)": 25}
    panel = next(p for p in importers.result_panels(r) if p["key"] == "stage_fallbacks")
    assert panel["groups"] == ["Phone Screen to Screening on Engineer (Acme): 25"]
    assert r["missing_stages"] == [{"job": "Engineer (Acme)", "stage": "Phone Screen"}]  # what ticking adds


# --------------------------------------------------------- 2. stage order
def test_added_stage_lands_where_the_file_puts_it(store):
    job = store.add_job("VP Sales", "Harbor", stages=["Sourced", "Screened", "Client interview", "Offer", "Placed",
                                                     "Rejected"])
    text = ("Name,Email,Job,Client,Status\n"
            "Ana,ana@x.com,VP Sales,Harbor,Screened\n"
            "Bo,bo@x.com,VP Sales,Harbor,Submitted\n"
            "Cy,cy@x.com,VP Sales,Harbor,Client interview\n"
            "Di,di@x.com,VP Sales,Harbor,Withdrawn\n")
    _run(store, text, backup=False)
    names = _stages(store, job["id"])
    assert names.index("Screened") < names.index("Submitted") < names.index("Client interview")
    assert names[-1] == "Withdrawn"  # a new closing stage goes at the end


def test_a_file_out_of_pipeline_order_gives_no_hint(store):
    job = store.add_job("Engineer", "Acme")
    text = ("Name,Email,Job,Client,Status\n"
            "Ana,ana@x.com,Engineer,Acme,Offer\n"
            "Bo,bo@x.com,Engineer,Acme,Panel\n"
            "Cy,cy@x.com,Engineer,Acme,Sourced\n")  # Offer before Sourced: sorted some other way
    _run(store, text, backup=False)
    names = _stages(store, job["id"])
    assert names.index("Panel") == names.index("Hired") - 1  # the usual spot, before the closing stages


def test_stage_dates_order_the_added_stages(store):
    job = store.add_job("Engineer", "Acme")
    text = ("Name,Email,Job,Client,Status,Stage date\n"
            "Ana,ana@x.com,Engineer,Acme,Onsite,2026-10-20\n"
            "Bo,bo@x.com,Engineer,Acme,Screening,2026-10-01\n"
            "Cy,cy@x.com,Engineer,Acme,Offer,2026-10-30\n")
    _run(store, text, backup=False)
    names = _stages(store, job["id"])
    assert names.index("Screening") < names.index("Onsite") < names.index("Offer")


# ------------------------------------------------------------ 3. dates
def test_date_columns_and_imported_marks(store):
    job = store.add_job("Engineer", "Acme")
    text = ("Name,Email,Job,Client,Status,Date Added,Last stage change\n"
            "Ana,ana@x.com,Engineer,Acme,Screening,2025-01-15,2026-09-01\n"
            "Bo,bo@x.com,Engineer,Acme,Hired,,\n")
    r, mapping = _run(store, text, backup=False)
    assert mapping[-2:] == ["created_at", "stage_changed_at"] and r["not_imported"] == []
    ana = store.find_duplicate("ana@x.com")
    assert ana["created_at"].startswith("2025-01-1")
    app = next(a for a in store.pipeline(job["id"]) if a["candidate_name"] == "Ana")
    assert app["stage_changed_at"].startswith("2026-09-0") or app["stage_changed_at"].startswith("2026-08-31")
    text_out = actions.job_status_text(store, job["id"], omit_stages=[])
    assert "Bo (imported)" in text_out and "(today)" not in text_out
    assert "Changes since last week: none." in text_out  # the import is not this week's news
    # A real move later counts again.
    store.move_stage(ana["id"], job["id"], "Interviewing", by="app")
    assert "Ana to Interviewing" in actions.job_status_text(store, job["id"], omit_stages=[])


def test_created_by_is_not_a_date(store):
    header, rows = importers.read_csv("Name,Created by,Added by\nAna,Marcus,Marcus\n")
    assert importers.guess_mapping(header, rows) == ["name", "", ""]


# ----------------------------------------------------- 4. Changes and backups
def test_web_import_is_one_row_on_changes_with_undo(store):
    store.add_candidate(name="Old Friend", email="old@x.com")
    c = local_client(store)
    text = "Name,Email\nAna,ana@x.com\nOld Friend,old@x.com\n"
    r = c.post("/import", files={"file": ("Connections.csv", text.encode())})
    assert 'name="filename" value="Connections.csv"' in r.text
    r = c.post("/import/run", data={"token": _token(r), "filename": "Connections.csv", "col0": "name", "col1": "email"})
    assert "Added 1" in r.text
    page = c.get("/changes").text
    assert "Imported Connections.csv: 2 rows, 1 new" in page and "Undo this import" in page
    listed = backups.list_backups()
    assert listed[0]["why"] == "Before importing Connections.csv (2 rows)"
    name = listed[0]["name"]
    r = c.post(f"/changes/import/{name}/undo", follow_redirects=False)
    assert r.status_code == 303 and store.find_duplicate("ana@x.com") is None


def test_changes_row_has_no_undo_after_other_edits(store):
    c = local_client(store)
    r = c.post("/import", files={"file": ("a.csv", b"Name,Email\nAna,ana@x.com\n")})
    c.post("/import/run", data={"token": _token(r), "filename": "a.csv", "col0": "name", "col1": "email"})
    store.add_candidate(name="Someone Else")
    page = c.get("/changes").text
    assert "Imported a.csv: 1 row, 1 new" in page and "Undo this import" not in page
    assert "can&#39;t be undone here" in page or "can't be undone here" in page


def test_no_op_import_takes_no_backup_and_keeps_older_ones(store):
    text = "Name,Email\nAna,ana@x.com\n"
    first, _ = _run(store, text, file_name="a.csv")
    assert first["backup"]
    for i in range(importers.BACKUPS_KEPT - 1):
        importers.backup_before_import(store)
    before = sorted(p.name for p in backups_dir().glob("before-import-*.db"))
    again, _ = _run(store, text, file_name="a.csv")
    assert again["backup"] is None and again["unchanged"] == 1
    assert sorted(p.name for p in backups_dir().glob("before-import-*.db")) == before
    backups.record_import(store, again, "a.csv", 1)
    assert backups.import_text(backups.import_log(store)[0]) == "Imported a.csv: 1 row, nothing changed"


def test_cli_import_is_recorded(store, tmp_path, monkeypatch):
    from talent import cli, paths

    f = tmp_path / "sync.csv"
    f.write_text("Name,Email\nAna,ana@x.com\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["talent", "import-csv", str(f)])
    cli.main()
    from talent.store import Store

    s = Store(paths.db_path())
    try:
        log = backups.import_log(s)
        assert log and log[0]["file"] == "sync.csv" and log[0]["added"] == 1
    finally:
        s.close()


# ------------------------------------------------------- 5. result panels
def test_already_in_gets_its_own_panel_and_long_lists_are_capped(store):
    job = store.add_job("Engineer", "Acme")
    for i in range(25):
        cid = store.add_candidate(name=f"P{i}", email=f"p{i}@x.com")["id"]
        store.add_to_pipeline(cid, job["id"], stage="Offer")
    text = "Name,Email,Job,Client,Status,Owner\n" + "".join(
        f"P{i},p{i}@x.com,Engineer,Acme,Screening,Marcus\n" for i in range(25))
    c = local_client(store)
    r = c.post("/import", files={"file": ("ats.csv", text.encode())})
    token = _token(r)
    r = c.post("/import/run", data={"token": token, "filename": "ats.csv", **_cols(
        ["name", "email", "job_title", "job_company", "stage", ""]), "add_stages": "1"})
    assert "Already in that job (25)" in r.text and "Imported, with a cell left blank" not in r.text
    assert "already in Engineer (Acme) at Offer, so they were left there: 25" in r.text
    assert "Download the full list (CSV)" in r.text and f"/import/{token}/result.csv?panel=already_in" in r.text
    assert "Not imported: Owner." in r.text
    csv_text = c.get(f"/import/{token}/result.csv?panel=already_in").text
    assert csv_text.count("already in Engineer (Acme) at Offer") == 25 and csv_text.startswith("Rows,")
    assert c.get(f"/import/{token}/result.csv?panel=nope").status_code == 404


def test_distinct_lines_are_capped_with_a_count():
    lines = [f"Row {i}: left Email blank, couldn't read {i}" for i in range(2, 30)]
    (panel,) = importers.result_panels({"warnings": lines})
    assert panel["groups"] == [] and len(panel["lines"]) == 20 and panel["more"] == 8


def test_settings_lists_the_labelled_backup(store):
    _run(store, "Name,Email\nAna,ana@x.com\n", file_name="weekly.csv")
    assert "Before importing weekly.csv (1 row)" in local_client(store).get("/settings").text


# ------------------------------------------------------- 6. tag columns
def test_skipped_column_can_become_a_tag(store):
    header, rows = importers.read_csv("Name,Email,Owner\nAna,ana@x.com,Marcus\n")
    assert "tag_column" in importers.IMPORT_TARGETS
    importers.import_rows(store, header, rows, ["name", "email", "tag_column"], backup=False)
    assert "Owner: Marcus" in store.find_duplicate("ana@x.com")["tags"]
    c = local_client(store)
    r = c.post("/import", files={"file": ("a.csv", b"Name,Owner\nBo,Marcus\n")})
    assert "Owner: Marcus" in r.text and 'value="tag_column"' in r.text


# ------------------------------------------------------- 7. big files
def test_big_file_says_it_takes_a_minute(store):
    text = "Name,Email\n" + "".join(f"P{i},p{i}@x.com\n" for i in range(1001))
    r = local_client(store).post("/import", files={"file": ("big.csv", text.encode())})
    assert "importing 1001 rows takes about a minute" in r.text and "data-busy=" in r.text
    assert "data-busy-note" in r.text
