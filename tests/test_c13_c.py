"""Cycle 13, builder C: the .xlsx writer, the Candidates table export, and the file shared with a hiring manager."""
import io
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from xml.etree import ElementTree as ET

import anyio
import pytest

from talent import xlsx, xlsx_export
from talent.mcp_server import build_server

from conftest import local_client

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}


def read_xlsx(data: bytes) -> dict:
    """A tiny reader: sheet name -> {rows: list of lists of text/number, freeze, filter, links}. Every part must parse."""
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.testzip() is None
    for n in z.namelist():
        if n.endswith(".xml") or n.endswith(".rels"):
            ET.fromstring(z.read(n))  # well formed
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = {}
    for sh in wb.find("m:sheets", NS):
        path = "xl/" + rels[sh.get(f"{{{NS['r']}}}id")]
        root = ET.fromstring(z.read(path))
        rel_path = path.replace("worksheets/", "worksheets/_rels/") + ".rels"
        urls = {}
        if rel_path in z.namelist():
            urls = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read(rel_path))}
        rows = []
        for row in root.find("m:sheetData", NS):
            cells = {}
            for c in row:
                col = re.match(r"[A-Z]+", c.get("r")).group()
                idx = sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(col))) - 1
                if c.get("t") == "inlineStr":
                    cells[idx] = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
                else:
                    cells[idx] = float(c.find("m:v", NS).text)
            rows.append([cells.get(i) for i in range(max(cells) + 1)] if cells else [])
        pane = root.find("m:sheetViews/m:sheetView/m:pane", NS)
        af = root.find("m:autoFilter", NS)
        links = {h.get("ref"): urls[h.get(f"{{{NS['r']}}}id")] for h in root.iter(f"{{{NS['m']}}}hyperlink")}
        out[sh.get("name")] = {"rows": rows, "freeze": None if pane is None else (pane.get("ySplit"), pane.get("state")),
                               "filter": None if af is None else af.get("ref"), "links": links}
    return out


# ------------------------------------------------------------------ the writer
def test_writer_parts_header_freeze_filter_links():
    data = xlsx.build_workbook([xlsx.Sheet("People", ["Name", "Site", "Years"],
                                           [["Ana", xlsx.Link("https://example.com/a?x=1&y=2", "Ana's site"), 4.5],
                                            ["Ben", xlsx.Link("javascript:alert(1)", "bad"), None]])])
    z = zipfile.ZipFile(io.BytesIO(data))
    assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/styles.xml", "xl/worksheets/sheet1.xml",
            "docProps/core.xml"} <= set(z.namelist())
    book = read_xlsx(data)["People"]
    assert book["rows"][0] == ["Name", "Site", "Years"]
    assert book["rows"][1] == ["Ana", "Ana's site", 4.5]
    assert book["freeze"] == ("1", "frozen") and book["filter"] == "A1:C3"
    assert book["links"] == {"B2": "https://example.com/a?x=1&y=2"}  # the unsafe link is only text
    assert "Talent Search" in z.read("docProps/core.xml").decode()
    assert b"lastModifiedBy" not in z.read("docProps/core.xml")


def test_writer_escapes_and_keeps_formulas_as_text():
    nasty = ["a<b>&\"'c", "x\x00y\x08z\x1fw", "=SUM(A1:A9)", "+1", "-2+3", "@cmd", "line1\r\nline2"]
    data = xlsx.build_workbook([xlsx.Sheet("S<&>/[x]", ["H"], [[v] for v in nasty])])
    z = zipfile.ZipFile(io.BytesIO(data))
    sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert "<f>" not in sheet and "t=\"inlineStr\"" in sheet
    assert not re.search("[\x00-\x08\x0b\x0c\x0e-\x1f]", sheet)
    (name, book), = read_xlsx(data).items()
    assert name == "S<&> x"  # characters Excel forbids in a sheet name become spaces
    got = [r[0] for r in book["rows"][1:]]
    assert got == ["a<b>&\"'c", "xyzw", "=SUM(A1:A9)", "+1", "-2+3", "@cmd", "line1\nline2"]


def test_sheet_names_unique_and_short():
    data = xlsx.build_workbook([xlsx.Sheet("A" * 50, ["h"], []), xlsx.Sheet("A" * 50, ["h"], [])])
    names = list(read_xlsx(data))
    assert len(names) == 2 and all(len(n) <= 31 for n in names) and len(set(n.lower() for n in names)) == 2


def test_writer_needs_a_sheet():
    with pytest.raises(ValueError):
        xlsx.build_workbook([])


def test_dates_are_real_cells():
    data = xlsx.build_workbook([xlsx.Sheet("D", ["When"], [[datetime(2026, 11, 10).date()]])])
    assert read_xlsx(data)["D"]["rows"][1] == [46336.0]


def test_preamble_moves_header_and_filter():
    data = xlsx.build_workbook([xlsx.Sheet("N", ["A", "B"], [["1", "2"]], preamble=["A note"])])
    b = read_xlsx(data)["N"]
    assert b["rows"][0] == ["A note"] and b["rows"][1] == ["A", "B"]
    assert b["filter"] == "A2:B3" and b["freeze"][0] == "2"


def _has_openpyxl():
    try:
        import openpyxl  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_openpyxl(), reason="openpyxl is not installed")
def test_openpyxl_opens_it():
    import openpyxl
    data = xlsx.build_workbook([xlsx.Sheet("P", ["Name", "Site"], [["=1+1", xlsx.Link("https://x.dev", "x")]])])
    ws = openpyxl.load_workbook(io.BytesIO(data)).active
    assert ws["A2"].value == "=1+1" and ws["A2"].data_type == "s"
    assert ws["B2"].hyperlink.target == "https://x.dev" and ws.freeze_panes == "A2" and ws.auto_filter.ref == "A1:B2"


@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice is not installed")
def test_libreoffice_converts_it(tmp_path):
    f = tmp_path / "t.xlsx"
    f.write_bytes(xlsx.build_workbook([xlsx.Sheet("P", ["Name"], [["Ana"], ["=1+1"]])]))
    run = subprocess.run(["soffice", "--headless", "--convert-to", "csv", "--outdir", str(tmp_path), str(f)],
                         capture_output=True, timeout=120)
    out = (tmp_path / "t.csv")
    assert run.returncode == 0 and out.exists()
    assert out.read_text().splitlines() == ["Name", "Ana", "=1+1"]


# ------------------------------------------------------------------ the app
def seed(store):
    job = store.add_job("Senior Backend Engineer", "Acme")
    other = store.add_job("Designer", "Acme")
    ana = store.add_candidate(name="Ana Ruiz", email="ana@example.com", phone="555-0101", current_title="Engineer",
                              current_company="Beta", location="Austin", tags=["python", "go"], color="red",
                              linkedin_url="https://www.linkedin.com/in/anaruiz",
                              website="https://ana.dev", about="Builds APIs.",
                              links=["https://github.com/ana", "http://127.0.0.1:8765/candidates/1"],
                              work_history=[{"title": "Engineer", "company": "Beta", "start": "2019-03", "end": "2023-05"},
                                            {"title": "Dev", "company": "Gamma", "start": "2016", "end": "2019"}])
    ben = store.add_candidate(name="Ben Cho", email="ben@example.com", current_title="Data Engineer",
                              tags=["python"], color="blue")
    cy = store.add_candidate(name="Cy =HYPERLINK(\"http://evil\")", tags=["rust"])
    store.add_to_pipeline(ana["id"], job["id"], "Interview")
    store.add_to_pipeline(ben["id"], other["id"], "Sourced")
    store.log(ana["id"], "note", "Private: asks too much money")
    return job, ana, ben, cy


def names_of(book):
    return [r[0] for r in book["Candidates"]["rows"][1:]]


def test_table_export_matches_table_filtered_and_hl_sorted(store):
    seed(store)
    client = local_client(store)
    params = {"tag": "python", "sort": "name", "dir": "asc", "hl": "go"}
    html = client.get("/candidates", params=params).text
    in_table = re.findall(r'href="/candidates/\d+">([^<]+)</a>', html)
    res = client.get("/candidates/export.xlsx", params=params)
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "talent-search-candidates-" in res.headers["content-disposition"]
    book = read_xlsx(res.content)
    assert names_of(book) == in_table == ["Ana Ruiz", "Ben Cho"]
    head = book["Candidates"]["rows"][0]
    assert {"Email", "Phone", "LinkedIn", "Skills", "Years of experience", "Other links"} <= set(head)
    assert book["Candidates"]["freeze"] and book["Candidates"]["filter"].startswith("A1:")
    ana = dict(zip(head, book["Candidates"]["rows"][1]))
    assert ana["Email"] == "ana@example.com" and ana["Skills"] == "python, go" and ana["Job"].startswith("Senior Backend")
    assert "127.0.0.1" not in ana["Other links"]


def test_table_export_search_order_and_formula_names(store):
    seed(store)
    client = local_client(store)
    book = read_xlsx(client.get("/candidates/export.xlsx", params={"q": "python", "hl": "go"}).content)
    assert names_of(book)[0] == "Ana Ruiz"  # the highlighted skill puts her first, as in the table
    allbook = read_xlsx(client.get("/export.xlsx").content)
    assert len(names_of(allbook)) == 3
    assert any(n.startswith("Cy =HYPERLINK") for n in names_of(allbook))  # stays text
    sheet = zipfile.ZipFile(io.BytesIO(client.get("/export.xlsx").content)).read("xl/worksheets/sheet1.xml").decode()
    assert "<f>" not in sheet


def test_table_export_says_when_cut_off(store):
    import talent.web as web
    for i in range(5):
        store.add_candidate(name=f"Person {i}")
    cap = web.BULK_MAX
    web.BULK_MAX = 3
    try:
        book = read_xlsx(local_client(store).get("/candidates/export.xlsx").content)
    finally:
        web.BULK_MAX = cap
    assert len(names_of(book)) == 3
    note = " ".join(str(r[1]) for r in book["About this export"]["rows"] if len(r) > 1)
    assert "first 3 of 5 people" in note


def test_downloads_are_not_for_other_sites(store):
    seed(store)
    client = local_client(store)
    for path in ("/export.xlsx", "/candidates/export.xlsx"):
        assert client.get(path, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.get(path, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_bulk_export_xlsx_for_ticked_people(store):
    job, ana, ben, cy = seed(store)
    res = local_client(store).post("/candidates/bulk", data={"action": "export_xlsx",
                                                              "candidate_ids": [str(ben["id"]), str(ana["id"])]})
    assert res.status_code == 200
    assert names_of(read_xlsx(res.content)) == ["Ben Cho", "Ana Ruiz"]  # the order they were ticked in


def test_buttons_on_the_table_page(store):
    seed(store)
    html = local_client(store).get("/candidates", params={"tag": "python", "hl": "go"}).text
    assert 'id="export-xlsx"' in html and "/candidates/export.xlsx?" in html and "hl=go" in html
    assert 'value="export_xlsx"' in html and 'id="share-xlsx"' in html and 'value="share"' in html
    assert "/export.xlsx" in local_client(store).get("/settings").text


def test_client_pipeline_xlsx(store):
    job, ana, ben, cy = seed(store)
    client = local_client(store)
    res = client.get("/clients/acme/pipeline.xlsx")
    assert res.status_code == 200
    book = read_xlsx(res.content)
    assert book["Pipeline"]["rows"][1][:2] == ["Job", "Candidate"]  # the title line comes first


# ------------------------------------------------------------------ sharing
def share(client, ids, **form):
    return client.post("/candidates/share", data={"ids": ",".join(map(str, ids)), **form})


def test_share_defaults_leave_out_contact_and_notes(store):
    job, ana, ben, cy = seed(store)
    res = share(local_client(store), [ana["id"], ben["id"]])
    assert res.status_code == 200
    assert "shortlist-" in res.headers["content-disposition"] and res.headers["content-disposition"].endswith('.xlsx"')
    raw = zipfile.ZipFile(io.BytesIO(res.content))
    blob = b"".join(raw.read(n) for n in raw.namelist())
    for secret in (b"ana@example.com", b"ben@example.com", b"555-0101", b"asks too much money", b"127.0.0.1", b"localhost"):
        assert secret not in blob
    book = read_xlsx(res.content)
    head = book["Candidates"]["rows"][0]
    assert head == ["Name", "Title", "Company", "Location", "Years of experience", "Skills", "LinkedIn", "Website",
                    "Other links", "About", "Work history"]
    row = dict(zip(head, book["Candidates"]["rows"][1]))
    assert row["Name"] == "Ana Ruiz" and row["Skills"] == "python, go" and row["About"] == "Builds APIs."
    assert row["Work history"] == "Engineer at Beta (2019 to 2023); Dev at Gamma (2016 to 2019)"
    assert row["Other links"] == "https://github.com/ana" and row["Years of experience"] > 6
    assert book["Candidates"]["links"]["G2"] == "https://linkedin.com/in/anaruiz"
    about = {r[0]: r[1] for r in book["About this file"]["rows"][1:]}
    assert about["People"] == 2.0 or about["People"] == "2"
    assert "email and phone" in about["Left out"] and "notes" in about["Left out"]
    assert "red" not in blob.decode("utf8", "ignore").lower().replace("shared", "")


def test_share_opt_ins_job_note_and_filename(store):
    job, ana, ben, cy = seed(store)
    res = share(local_client(store), [ana["id"], ben["id"]], include_contact="1", include_notes="1",
                job_id=str(job["id"]), note="Shortlist for the backend role")
    today = datetime.now().strftime("%Y-%m-%d")
    assert f'filename="shortlist-acme-senior-backend-engineer-{today}.xlsx"' in res.headers["content-disposition"]
    book = read_xlsx(res.content)
    rows = book["Candidates"]["rows"]
    assert rows[0] == [f"Shortlist for the backend role, as of {today}"] and rows[1][0] == "Name"
    head = rows[1]
    assert {"Stage", "Email", "Phone", "Notes"} <= set(head)
    a, b = (dict(zip(head, r)) for r in rows[2:4])
    assert a["Email"] == "ana@example.com" and a["Phone"] == "555-0101" and a["Stage"] == "Interviewing"
    assert "asks too much money" in a["Notes"]
    assert b["Stage"] is None  # not in the job: blank
    assert book["Candidates"]["freeze"][0] == "2"


def test_share_job_without_note_gets_a_default_note(store):
    job, ana, ben, cy = seed(store)
    rows = read_xlsx(share(local_client(store), [ana["id"]], job_id=str(job["id"])).content)["Candidates"]["rows"]
    today = datetime.now().strftime("%Y-%m-%d")
    assert rows[0] == [f"Shortlist for Senior Backend Engineer, as of {today}"]


def test_share_page_takes_ids_and_defaults_off(store):
    job, ana, ben, cy = seed(store)
    client = local_client(store)
    html = client.get(f"/candidates/share?ids={ana['id']},{ben['id']}").text
    assert "Ana Ruiz" in html and 'name="ids" value="%d,%d"' % (ana["id"], ben["id"]) in html
    assert 'name="include_contact" value="1" checked' not in html and 'name="include_notes" value="1" checked' not in html
    assert "Senior Backend Engineer" in html
    assert "Tick people" in client.get("/candidates/share").text


def test_bulk_share_opens_the_options_for_all_results(store):
    job, ana, ben, cy = seed(store)
    res = local_client(store).post("/candidates/bulk", data={"action": "share", "select_all": "1", "f_tag": "python"})
    assert res.status_code == 200 and "Ana Ruiz" in res.text and "Ben Cho" in res.text and "Make the spreadsheet" in res.text
    res = local_client(store).post("/candidates/bulk", data={"action": "share", "candidate_ids": [str(cy["id"])]})
    assert 'name="ids" value="%d"' % cy["id"] in res.text


def test_share_with_nobody_is_a_friendly_redirect(store):
    seed(store)
    res = local_client(store).post("/candidates/share", data={"ids": ""}, follow_redirects=False)
    assert res.status_code == 303


# ------------------------------------------------------------------ in chat
def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def data_of(result):
    return result.structuredContent if hasattr(result, "structuredContent") else result[1].get("result", result[1])


def test_mcp_tool_writes_a_file_without_private_fields(store, tmp_path, monkeypatch):
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(tmp_path / "dl"))
    job, ana, ben, cy = seed(store)
    data = data_of(call(build_server(store), "export_candidates_xlsx", candidate_ids=[ana["id"], ben["id"]], job_id=job["id"]))
    assert data["written"] and data["people"] == 2 and "/candidates/share?" in data["share_url"]
    f = tmp_path / "dl" / data["file"]
    assert f.exists() and data["file"].startswith("shortlist-acme-senior-backend-engineer-")
    blob = b"".join(zipfile.ZipFile(f).read(n) for n in zipfile.ZipFile(f).namelist())
    assert b"ana@example.com" not in blob and b"asks too much money" not in blob
    again = data_of(call(build_server(store), "export_candidates_xlsx", candidate_ids=[ana["id"]]))
    assert again["path"] != str(f) or again["file"] != data["file"]
    assert (tmp_path / "dl" / again["file"]).exists()


def test_mcp_tool_needs_the_recruiter_for_contact_and_notes(store, tmp_path, monkeypatch):
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(tmp_path / "dl"))
    job, ana, ben, cy = seed(store)
    data = data_of(call(build_server(store), "export_candidates_xlsx", candidate_ids=[ana["id"]], include_contact=True))
    assert data["written"] is False and "contact=1" in data["share_url"]
    assert not (tmp_path / "dl").exists() or not list((tmp_path / "dl").iterdir())


def _set(store, key, value):
    with store._conn:
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def test_mcp_tool_waits_in_cautious_mode_and_checks_ids(store, tmp_path, monkeypatch):
    from talent import review
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(tmp_path / "dl"))
    job, ana, ben, cy = seed(store)
    _set(store, review.SETTING, "1")
    assert data_of(call(build_server(store), "export_candidates_xlsx", candidate_ids=[ana["id"]]))["written"] is False
    _set(store, review.SETTING, "0")
    with pytest.raises(Exception):
        call(build_server(store), "export_candidates_xlsx", candidate_ids=[])
    with pytest.raises(Exception):
        call(build_server(store), "export_candidates_xlsx", candidate_ids=[99999])


def test_people_view_has_export_button():
    from pathlib import Path
    views = Path(__file__).resolve().parent.parent / "src/talent/static/mcp_views"
    js, kit = (views / "people.js").read_text(), (views / "kit.js").read_text()  # the call itself now lives in kit.js (exportIds)
    assert 'export_candidates_xlsx' in kit and '"Export "' in js and "exportIds(" in js
    assert "innerHTML" not in js.split("function exportXlsx")[1].split("function draw")[0]
