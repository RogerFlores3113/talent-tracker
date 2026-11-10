"""Cycle 13, fixer F2: print setup, the share file made ready to forward, highlights in the files, the share form,
caps, the CSV beside the xlsx, and the client pipeline sheet."""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, datetime

from conftest import local_client
from test_c13_c import read_xlsx, seed

from talent import xlsx, xlsx_export


def parts(data: bytes) -> dict[str, str]:
    z = zipfile.ZipFile(io.BytesIO(data))
    return {n: z.read(n).decode("utf8") for n in z.namelist()}


def share(client, ids, **form):
    return client.post("/candidates/share", data={"ids": ",".join(map(str, ids)), **form})


def table(book, sheet="Candidates", skip=0):
    rows = book[sheet]["rows"][skip:]
    head = rows[0]
    return head, [dict(zip(head, r)) for r in rows[1:]]


# ---------------------------------------------------------------- 1. print setup
def test_every_sheet_has_print_setup():
    data = xlsx.build_workbook([xlsx.Sheet("A", ["x", "y"], [["1", "2"]], preamble=["Note"]),
                                xlsx.Sheet("It's", ["h"], [])])
    p = parts(data)
    for n in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
        assert 'orientation="landscape"' in p[n] and 'fitToWidth="1"' in p[n] and 'fitToHeight="0"' in p[n]
        assert '<pageSetUpPr fitToPage="1"/>' in p[n] and 'left="0.4"' in p[n]
    wb = p["xl/workbook.xml"]
    assert "_xlnm.Print_Titles" in wb and "'A'!$2:$2" in wb and "'It''s'!$1:$1" in wb


# ---------------------------------------------------------------- 2. the share file
def test_share_drops_columns_empty_for_everyone_and_keeps_given_order(store):
    job, ana, ben, cy = seed(store)
    book = read_xlsx(share(local_client(store), [cy["id"], ben["id"], ana["id"]]).content)
    head, rows = table(book)
    assert [r["Name"] for r in rows] == [cy["name"], "Ben Cho", "Ana Ruiz"]  # the order the ids came in
    assert "Website" in head and "About" in head
    only_ben = read_xlsx(share(local_client(store), [ben["id"], cy["id"]]).content)
    head2, _ = table(only_ben)
    assert "Website" not in head2 and "About" not in head2 and "Work history" not in head2 and "LinkedIn" not in head2


def test_share_note_line_is_bold_merged_and_wrapped(store):
    job, ana, ben, cy = seed(store)
    res = share(local_client(store), [ana["id"], ben["id"]], note="A long note " * 20)
    p = parts(res.content)
    sheet = p["xl/worksheets/sheet1.xml"]
    assert re.search(r'<mergeCell ref="A1:[A-Z]+1"/>', sheet)
    assert 'customHeight="1"' in sheet.split("<row r=\"2\"")[0]
    assert '<b/><sz val="12"/>' in p["xl/styles.xml"]


# ---------------------------------------------------------------- 3 + 4. highlights
def test_share_with_highlights_adds_matched_skills_in_color(store):
    job, ana, ben, cy = seed(store)
    res = share(local_client(store), [ben["id"], ana["id"]], hl="python:red,go:yellow")
    p = parts(res.content)
    book = read_xlsx(res.content)
    head, rows = table(book)
    assert head[:2] == ["Name", "Matched picks"]
    assert rows[0]["Matched picks"] == "python (1 of 2)" and rows[1]["Matched picks"] == "python, go (2 of 2)"
    assert "FFC62828" in p["xl/styles.xml"] and "FFF6D53D" in p["xl/styles.xml"]  # the app's red and yellow
    about = {r[0]: r[1] for r in book["About this file"]["rows"][1:]}
    assert about["Highlighted picks"] == "python (red), go (yellow)"
    assert "hl:" not in str(about)


def test_table_export_highlights_and_filters_in_words(store):
    seed(store)
    client = local_client(store)
    res = client.get("/candidates/export.xlsx", params={"tag": "python", "location": "Austin", "hl": "python:red,go:yellow"})
    book = read_xlsx(res.content)
    head, rows = table(book)
    assert head[1] == "Matched picks" and rows[0]["Matched picks"] == "python, go (2 of 2)"
    about = {r[0]: r[1] for r in book["About this export"]["rows"][1:]}
    assert about["Highlighted picks"] == "python (red), go (yellow)"
    assert about["Location"] == "Austin" and about["Skill"] == "python"
    assert "Notes" in about["Not in this file"] and "hl" not in about
    plain = read_xlsx(client.get("/candidates/export.xlsx").content)
    assert "Matched picks" not in table(plain)[0]


# ---------------------------------------------------------------- 5. dates, years, label color
def test_real_dates_half_years_and_label_color(store):
    seed(store)
    data = local_client(store).get("/export.xlsx").content
    p = parts(data)
    assert 'formatCode="yyyy\\-mm\\-dd"' in p["xl/styles.xml"]
    book = read_xlsx(data)
    head, rows = table(book)
    ana = next(r for r in rows if r["Name"] == "Ana Ruiz")
    assert isinstance(ana["Updated"], float) and isinstance(ana["Added"], float)  # real dates, not text
    assert ana["Years of experience"] * 2 == int(ana["Years of experience"] * 2)  # whole or half years
    assert "Label color" in head and "Color" not in head
    plain = local_client(store)
    for c in store.all_candidates():
        store.update_candidate(c["id"], color="") if c.get("color") else None
    head2, _ = table(read_xlsx(plain.get("/export.xlsx").content))
    assert "Label color" not in head2


def test_half_years():
    assert xlsx_export.half_years(6.74) == 6.5 and xlsx_export.half_years(6.76) == 7.0 and xlsx_export.half_years(None) is None


# ---------------------------------------------------------------- 6. a job picked
def test_share_job_blank_stage_rejected_left_out_and_stage_order(store):
    job, ana, ben, cy = seed(store)
    dee = store.add_candidate(name="Dee Lo", tags=["go"])
    eli = store.add_candidate(name="Eli Re", tags=["go"])
    store.add_to_pipeline(dee["id"], job["id"], "Sourced")
    store.add_to_pipeline(eli["id"], job["id"], "Rejected")
    client = local_client(store)
    ids = [dee["id"], ben["id"], eli["id"], ana["id"]]
    book = read_xlsx(share(client, ids, job_id=str(job["id"])).content)
    head, rows = table(book, skip=1)
    assert [r["Name"] for r in rows] == ["Ana Ruiz", "Dee Lo"]  # furthest along first; Ben isn't in the job (cycle 17)
    assert [r.get("Stage") for r in rows] == ["Interviewing", "Sourced"]
    about = {r[0]: r[1] for r in book["About this file"]["rows"][1:]}
    assert "1 person rejected or withdrawn in this job" in about["People left out"]
    assert "1 person not in this job" in about["People left out"]
    book = read_xlsx(share(client, ids, job_id=str(job["id"]), include_rejected="1").content)
    names = [r["Name"] for r in table(book, skip=1)[1]]
    assert names == ["Ana Ruiz", "Dee Lo", "Eli Re"]
    # no job: the ids order
    book = read_xlsx(share(client, ids).content)
    assert [r["Name"] for r in table(book)[1]] == ["Dee Lo", "Ben Cho", "Eli Re", "Ana Ruiz"]


def test_share_notes_choices(store):
    job, ana, ben, cy = seed(store)
    store.log(ana["id"], "note", "Second note, newest")
    client = local_client(store)
    none = read_xlsx(share(client, [ana["id"]]).content)
    assert "Notes" not in table(none)[0]
    latest = read_xlsx(share(client, [ana["id"]], notes="latest").content)
    assert table(latest)[1][0]["Notes"] == "Second note, newest"
    everything = read_xlsx(share(client, [ana["id"]], notes="all").content)
    n = table(everything)[1][0]["Notes"]
    assert "Second note, newest" in n and "asks too much money" in n
    legacy = read_xlsx(share(client, [ana["id"]], include_notes="1").content)
    assert "asks too much money" in table(legacy)[1][0]["Notes"]


# ---------------------------------------------------------------- 7. the form
def test_share_form_wording_radios_preview_and_live_line(store):
    job, ana, ben, cy = seed(store)
    html = local_client(store).get(f"/candidates/share?ids={ana['id']},{ben['id']}&hl=python:red").text
    assert "About and work history go in as written, except that email addresses and phone numbers in them are taken out" in html
    assert 'type="radio" name="notes" value="none" checked' in html
    assert 'value="latest"' in html and 'value="all"' in html and "Latest note only" in html
    assert "All notes and timeline" in html and "No notes" in html and "Notes go out word for word and may hold pay" in html
    assert 'name="hl" value="python:red"' in html and 'name="include_rejected"' in html
    assert re.search(r'id="pv-file">shortlist-\d{4}-\d{2}-\d{2}\.xlsx<', html)
    assert "Ana Ruiz, Ben Cho" in html and "Matched picks" in html
    assert 'id="share-live"' in html and 'aria-live="polite"' in html
    js = local_client(store).get("/static/share.js").text
    assert "Your spreadsheet is downloading. Look in your Downloads folder." in js
    assert "fetch(" not in js and "XMLHttpRequest" not in js and "innerHTML" not in js
    job_html = local_client(store).get(f"/candidates/share?ids={ana['id']}&job={job['id']}").text
    assert "shortlist-acme-senior-backend-engineer-" in job_html


def test_share_filename_rules():
    now = datetime(2026, 11, 10)
    assert xlsx_export.share_filename(None, now) == "shortlist-2026-11-10.xlsx"
    assert xlsx_export.share_filename({"title": "Senior Backend Engineer"}, now) == "shortlist-senior-backend-engineer-2026-11-10.xlsx"
    assert xlsx_export.share_filename(None, now, "Shortlist for the backend role") == "shortlist-2026-11-10.xlsx"
    assert xlsx_export.share_filename(None, now, "Top picks") == "shortlist-2026-11-10.xlsx"


# ---------------------------------------------------------------- 8. caps
def test_bulk_use_all_keeps_the_first_of_note(store):
    import talent.web as web
    for i in range(5):
        store.add_candidate(name=f"Person {i}", tags=["zz"])
    cap = web.BULK_MAX
    web.BULK_MAX = 3
    try:
        res = local_client(store).post("/candidates/bulk", data={"action": "export_xlsx", "select_all": "1", "f_tag": "zz"})
        page = local_client(store).get("/candidates").text
    finally:
        web.BULK_MAX = cap
    book = read_xlsx(res.content)
    note = " ".join(str(r[1]) for r in book["About this export"]["rows"] if len(r) > 1)
    assert "first 3 of 5 people" in note
    assert "Exports hold the first 3 of 5 people." in page


def test_no_cap_line_when_everything_fits(store):
    seed(store)
    assert "Exports hold the first" not in local_client(store).get("/candidates").text


def test_settings_label_has_the_count(store):
    seed(store)
    assert "Export all 3 people as xlsx" in local_client(store).get("/settings").text


# ---------------------------------------------------------------- 9. the CSV beside the xlsx
def test_candidates_export_csv_matches_the_xlsx(store):
    job, ana, ben, cy = seed(store)
    client = local_client(store)
    params = {"tag": "python", "sort": "name", "dir": "desc", "hl": "go"}
    html = client.get("/candidates", params=params).text
    assert 'id="export-csv" href="/candidates/export.csv?' in html
    res = client.get("/candidates/export.csv", params=params)
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
    names = [r[0] for r in rows[1:]]
    xl = [r["Name"] for r in table(read_xlsx(client.get("/candidates/export.xlsx", params=params).content))[1]]
    assert names == xl == ["Ana Ruiz", "Ben Cho"]
    assert client.get("/candidates/export.csv", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    store.add_candidate(name="=SUM(1+1)")
    whole = client.get("/export.csv").content.decode("utf-8-sig")
    assert any(r[0] == "'=SUM(1+1)" for r in csv.reader(io.StringIO(whole)))
    assert any(r[0] == "'=SUM(1+1)" for r in csv.reader(io.StringIO(client.get("/candidates/export.csv").content.decode("utf-8-sig"))))
    xl = table(read_xlsx(client.get("/candidates/export.xlsx").content))[1]
    assert any(r["Name"] == "=SUM(1+1)" for r in xl)  # the xlsx keeps it as text


def test_csv_in_order_keeps_the_formula_guard(store):
    job, ana, ben, cy = seed(store)
    eq = store.add_candidate(name="=1+1")
    data = xlsx_export.csv_in_order(store, [eq["id"], ana["id"]]).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(data)))
    assert rows[1][0] == "'=1+1" and rows[2][0] == "Ana Ruiz"


# ---------------------------------------------------------------- 10. client pipeline
def test_client_pipeline_title_dates_and_print(store):
    job, ana, ben, cy = seed(store)
    res = local_client(store).get("/clients/acme/pipeline.xlsx")
    book = read_xlsx(res.content)
    rows = book["Pipeline"]["rows"]
    assert re.fullmatch(r"Acme pipeline, as of \d{4}-\d{2}-\d{2}", rows[0][0])
    assert rows[1][0] == "Job" and rows[1][-1].startswith("Next interview (")
    p = parts(res.content)["xl/worksheets/sheet1.xml"]
    assert 'orientation="landscape"' in p and "<mergeCell" in p
    row = dict(zip(rows[1], rows[2]))
    assert isinstance(row["In stage since"], float)  # a real date


def test_typed_pipeline_has_real_date_times(store):
    from talent import reports
    rep = {"client": {"time_zone": "America/New_York"}, "inhouse": False,
           "upcoming": [{"candidate_id": 1, "job": "J", "at": "2026-11-12T15:00:00+00:00", "at_client": "x"}],
           "people": [{"candidate_id": 1, "job": "J", "name": "A", "group": "interview", "stage": "Interviewing",
                       "since": "2026-11-01T12:00:00+00:00", "days": 9, "imported": False, "submitted_at": None}]}
    head, rows = reports.pipeline_table(rep, typed=True)
    assert head[-1] == "Next interview (America/New_York)"
    assert rows[0][-1] == datetime(2026, 11, 12, 10, 0) and rows[0][6] == date(2026, 11, 1)
    data = xlsx.build_workbook([xlsx.Sheet("P", head, rows)])
    assert 'formatCode="yyyy\\-mm\\-dd\\ hh:mm"' in parts(data)["xl/styles.xml"]
