"""c14 D3: privacy, files and web polish: share notes per person, leave a person out, hiring manager view, client
status text defaults, CSV dates and notes option, xlsx date cells, bulk bar, board layout, /today."""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime
from pathlib import Path

from conftest import local_client
from talent import actions, xlsx_export
from test_c13_c import read_xlsx

ROOT = Path(__file__).resolve().parent.parent
TODAY = datetime.now().strftime("%Y-%m-%d")


def seed(store):
    job = store.add_job("Senior Backend Engineer", "Acme Payments")
    ana = store.add_candidate(name="Ana Ruiz", email="ana@example.com", tags=["python"], about="Builds APIs.")
    ben = store.add_candidate(name="Ben Cho", tags=["go"], about="Writes Go.")
    cy = store.add_candidate(name="Cy Dale", tags=["rust"])
    store.add_to_pipeline(ana["id"], job["id"], "Interview")
    store.add_to_pipeline(ben["id"], job["id"], "Interview")
    store.log(ana["id"], "note", "PRIVATE: wants 240k, also interviewing at Stripe and a very long tail " + "x" * 80)
    store.log(ben["id"], "note", "Ben is calm and clear")
    return job, ana, ben, cy


def share(client, ids, **form):
    return client.post("/candidates/share", data={"ids": ",".join(map(str, ids)), **form})


# ------------------------------------------------------------------ 1. share form
def test_share_form_warns_and_previews_each_note(store):
    job, ana, ben, cy = seed(store)
    html = local_client(store).get(f"/candidates/share?ids={ana['id']},{ben['id']}&notes=latest").text
    assert "Notes can hold pay talk and private remarks. They go into the file as written." in html
    assert 'class="share-red"' in html
    assert "PRIVATE: wants 240k" in html and "Ben is calm and clear" in html
    cut = "PRIVATE: wants 240k, also interviewing at Stripe and a very long tail " + "x" * 80
    assert cut[:80] in html and cut[:81] not in html  # first 80 characters only
    assert html.count('name="leave_note"') == 2 and html.count('name="leave_out"') == 2
    assert "Leave this note out" in html and "Leave this person out" in html
    assert "<dt>People</dt>" in html and "First names" not in html


def test_share_leave_note_and_leave_person_are_honored(store):
    job, ana, ben, cy = seed(store)
    c = local_client(store)
    res = share(c, [ana["id"], ben["id"], cy["id"]], notes="latest", leave_note=str(ana["id"]), leave_out=str(cy["id"]))
    rows = read_xlsx(res.content)["Candidates"]["rows"]
    head = rows[0]
    by = {r[0]: dict(zip(head, r)) for r in rows[1:]}
    assert set(by) == {"Ana Ruiz", "Ben Cho"}  # Cy is out
    assert not by["Ana Ruiz"].get("Notes") and by["Ben Cho"]["Notes"] == "Ben is calm and clear"
    assert b"PRIVATE" not in res.content and b"Stripe" not in res.content


def test_share_everyone_left_out_asks_again(store):
    job, ana, ben, cy = seed(store)
    res = share(local_client(store), [ana["id"]], leave_out=str(ana["id"]))
    assert res.headers["content-type"].startswith("text/html") and "Everyone is left out" in res.text


def test_share_says_when_nobody_matches_highlights(store):
    job, ana, ben, cy = seed(store)
    c = local_client(store)
    html = c.get(f"/candidates/share?ids={ana['id']},{ben['id']}&hl=cobol:red").text
    assert "Nobody here has the highlighted picks, so that column is left out." in html
    ok = c.get(f"/candidates/share?ids={ana['id']},{ben['id']}&hl=python:red").text
    assert "Nobody here has the highlighted" not in ok and "Matched picks column" in ok


def test_share_defaults_to_candidates_job_filter_and_names_company(store):
    job, ana, ben, cy = seed(store)
    c = local_client(store)
    page = c.post("/candidates/bulk", data={"action": "share", "candidate_ids": [ana["id"], ben["id"]],
                                            "f_job": str(job["id"])}).text
    assert f'value="{job["id"]}" data-title="Senior Backend Engineer" data-company="Acme Payments" selected' in page
    assert f"shortlist-acme-payments-senior-backend-engineer-{TODAY}.xlsx" in page
    res = share(c, [ana["id"]], job_id=str(job["id"]), note="Pay is 250k, secret")
    assert f'filename="shortlist-acme-payments-senior-backend-engineer-{TODAY}.xlsx"' in res.headers["content-disposition"]
    assert "250k" not in res.headers["content-disposition"]
    assert xlsx_export.share_filename({"title": "Lead", "company": ""}, datetime(2026, 11, 10)) == "shortlist-lead-2026-11-10.xlsx"
    js = c.get("/static/share.js").text
    assert "data-company" in js


def test_share_title_row_says_as_of(store):
    job, ana, ben, cy = seed(store)
    res = share(local_client(store), [ana["id"]], job_id=str(job["id"]))
    assert read_xlsx(res.content)["Candidates"]["rows"][0] == [f"Shortlist for Senior Backend Engineer, as of {TODAY}"]


# ------------------------------------------------------------------ 2. hiring manager view
def hm(store, ids, extra=""):
    return local_client(store).get(f"/candidates/compare?ids={','.join(map(str, ids))}&view=hm{extra}").text


def test_hm_view_not_in_play_headings_landmarks_and_title(store):
    job = store.add_job("Designer", "Acme")
    live = store.add_job("Data Engineer", "Acme")
    a = store.add_candidate(name="Maya Ross", about="About Maya")
    b = store.add_candidate(name="Sofia Lane", about="About Sofia")
    store.add_to_pipeline(a["id"], live["id"], "Interview")
    store.add_to_pipeline(b["id"], job["id"], "Rejected")
    html = hm(store, [a["id"], b["id"]])
    assert "<title>Hiring manager comparison" in html and "<h1" in html and "Hiring manager comparison</h1>" in html
    assert "data-focus-here" in html.split("<h1", 1)[1].split(">", 1)[0]
    assert "1 person left out: not in play in this job." in html
    assert '<span class="muted">Not in play</span>' in html and "Rejected" not in html and "Designer" not in html
    assert len(re.findall(r'<h2 class="person-h">', html)) == 2
    assert "<header" in html and "<main" in html and "<footer" in html


def test_hm_view_hides_about_per_person(store):
    a = store.add_candidate(name="Maya Ross", about="Secret about Maya")
    b = store.add_candidate(name="Noah Park", about="Open about Noah")
    html = hm(store, [a["id"], b["id"]], f"&noabout={a['id']}")
    assert "Secret about Maya" not in html and "Open about Noah" in html and "About hidden" in html
    assert f'name="noabout" value="{a["id"]}" checked' in html
    assert f'name="noabout" value="{b["id"]}" checked' not in html
    assert "Hide About for the people ticked" in html and "Hide About" in html
    both = hm(store, [a["id"], b["id"]], "&about=0")
    assert "Secret about Maya" not in both and "Open about Noah" not in both


# ------------------------------------------------------------------ 3. client status text
def test_status_text_leaves_out_rejected_and_names_who_is_in(store):
    job = store.add_job("Staff Backend Engineer", company="Acme")
    for stage, name in (("Interview", "Ana Ruiz"), ("Rejected", "Sofia Example"), ("Withdrawn", "Wes Gone"),
                        ("Sourced", "Sam Cold")):
        c = store.add_candidate(name=name)
        store.add_to_pipeline(c["id"], job["id"], stage)
    html = local_client(store).get(f"/jobs/{job['id']}").text
    text = html.split('<pre class="code status-text">', 1)[1].split("</pre>", 1)[0]
    assert "Ana Ruiz" in text
    for gone in ("Sofia", "Wes Gone", "Sam Cold"):
        assert gone not in text
    assert "Names that will be sent (1): Ana Ruiz." in html
    for stage in ("Rejected", "Withdrawn", "Sourced"):
        assert f'value="{stage}" checked' in html
    # their own choice wins once saved
    local_client(store).post(f"/jobs/{job['id']}/omit", data={})
    html = local_client(store).get(f"/jobs/{job['id']}").text
    assert "Sofia Example" in html.split('id="status-names"', 1)[1]


# ------------------------------------------------------------------ 4. files
def test_csv_has_plain_dates_and_no_notes_by_default(store):
    job, ana, ben, cy = seed(store)
    c = local_client(store)
    plain = c.get("/candidates/export.csv").content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(plain)))
    assert "Notes" not in rows[0] and "PRIVATE" not in plain and "Stripe" not in plain
    for r in rows:
        for col in ("Added", "Last updated"):
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", r[col]), r[col]
    full = c.get("/candidates/export.csv?notes=1").content.decode("utf-8-sig")
    assert "PRIVATE" in full and "Notes" in next(csv.reader(io.StringIO(full)))
    bulk = c.post("/candidates/bulk", data={"action": "export", "candidate_ids": [ana["id"]]}).content.decode("utf-8-sig")
    assert "PRIVATE" not in bulk
    bulk = c.post("/candidates/bulk", data={"action": "export", "candidate_ids": [ana["id"]], "include_notes": "1"}).content.decode("utf-8-sig")
    assert "PRIVATE" in bulk
    from talent import importers  # the full backup keeps everything
    assert "PRIVATE" in importers.export_csv(store)
    page = c.get("/candidates").text
    assert 'id="export-csv-notes"' in page and "Include notes" in page


def test_table_xlsx_dates_are_real_date_cells(store):
    seed(store)
    data = local_client(store).get("/candidates/export.xlsx").content
    z = zipfile.ZipFile(io.BytesIO(data))
    styles = z.read("xl/styles.xml").decode()
    assert 'formatCode="yyyy\\-mm\\-dd"' in styles
    sheet = z.read("xl/worksheets/sheet1.xml").decode()
    book = read_xlsx(data)["Candidates"]["rows"]
    head = book[0]
    col = head.index("Updated")
    letter = chr(ord("A") + col)
    cells = re.findall(rf'<c r="{letter}\d+" s="(\d+)"><v>([\d.]+)</v>', sheet)
    assert cells and all(s == "3" and float(v).is_integer() for s, v in cells)  # style 3 = yyyy-mm-dd, no time part


# ------------------------------------------------------------------ 5. web a11y and layout
def test_bulk_bar_has_count_collapsed_panel_and_message(store):
    seed(store)
    html = local_client(store).get("/candidates").text
    assert 'id="bulk-count"' in html and 'role="status"' in html and "0 people ticked" in html
    assert 'id="bulk-panel"' in html and 'id="bulk-msg"' in html and 'role="alert"' in html
    assert 'id="export-status"' in html
    js = local_client(store).get("/static/app.js").text
    assert "people ticked" in js and "preventDefault" in js and "Your spreadsheet is downloading" in js
    assert "innerHTML" not in js.split("Candidates table, cycle 14", 1)[1]


def test_board_columns_are_not_closed_early(store):
    job = store.add_job("Dev", "Acme")
    for i, st in enumerate(("Sourced", "Contacted", "Interview")):
        c = store.add_candidate(name=f"P{i} Person")
        store.add_to_pipeline(c["id"], job["id"], st)
    html = local_client(store).get(f"/jobs/{job['id']}?view=board").text
    board = html.split('<div class="board">', 1)[1]
    first_col = board.split("</section>", 1)[0]
    assert first_col.count("<div") == first_col.count("</div>")  # a stray </div> ended the board after one column
    assert board.count('<section class="col') >= 3
    inside = board.split('class="board-end"', 1)[0] if "board-end" in board else board
    assert inside.index("</div>") > inside.index('data-stage="Sourced"')


def test_today_redirects_home(store):
    res = local_client(store).get("/today", follow_redirects=False)
    assert res.status_code in (301, 302, 303, 307, 308) and res.headers["location"] == "/"


def test_jobs_switch_has_its_own_space(store):
    store.add_job("Dev", "Acme")
    css = (ROOT / "src/talent/static/style.css").read_text()
    assert re.search(r"form\.wording\s*\{[^}]*margin-top", css)
