"""Cycle 13, final fixer G1: the Candidates table bulk actions and exports."""
from __future__ import annotations

import csv
import io
import re
from urllib.parse import parse_qs, urlsplit

from conftest import local_client
from test_c13_c import read_xlsx

from talent import importers, xlsx_export


def three(store):
    a = store.add_candidate(name="Ana Ruiz", tags=["python", "sql"], email="ana@example.com")
    b = store.add_candidate(name="Ben Cho", tags=["python"])
    c = store.add_candidate(name="Cy Dunn", tags=["rust"])
    return a["id"], b["id"], c["id"]


def csv_rows(text):
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


def xlsx_names(data):
    return [r[0] for r in read_xlsx(data)["Candidates"]["rows"][1:]]


def test_bulk_keeps_screen_order_everywhere(store):
    a, b, c = three(store)
    client = local_client(store)
    order = [c, a, b]  # ids 3, 1, 2 as the boxes come
    r = client.post("/candidates/bulk", data={"action": "compare", "candidate_ids": order + [a]}, follow_redirects=False)
    assert parse_qs(urlsplit(r.headers["location"]).query)["ids"] == ["3,1,2"]
    r = client.post("/candidates/bulk", data={"action": "share", "candidate_ids": order})
    assert re.findall(r'name="ids" value="([^"]*)"', r.text) == ["3,1,2"]
    r = client.post("/candidates/bulk", data={"action": "export", "candidate_ids": order})
    assert [x["Name"] for x in csv_rows(r.text)] == ["Cy Dunn", "Ana Ruiz", "Ben Cho"]
    r = client.post("/candidates/bulk", data={"action": "export_xlsx", "candidate_ids": order})
    assert xlsx_names(r.content) == ["Cy Dunn", "Ana Ruiz", "Ben Cho"]


def test_bulk_csv_and_xlsx_same_people_same_order_with_matched_skills(store):
    a, b, c = three(store)
    client = local_client(store)
    form = {"candidate_ids": [b, a, c], "f_hl": "python:red,sql:blue"}
    rc = client.post("/candidates/bulk", data={"action": "export", **form})
    rx = client.post("/candidates/bulk", data={"action": "export_xlsx", **form})
    rows = csv_rows(rc.text)
    assert [x["Name"] for x in rows] == xlsx_names(rx.content) == ["Ben Cho", "Ana Ruiz", "Cy Dunn"]
    assert [x["Matched skills"] for x in rows] == ["python (1 of 2)", "python, sql (2 of 2)", ""]
    book = read_xlsx(rx.content)["Candidates"]["rows"]
    assert [r[1] for r in book[1:]] == ["python (1 of 2)", "python, sql (2 of 2)", None]
    plain = client.post("/candidates/bulk", data={"action": "export", "candidate_ids": [b, a]})
    assert "Matched skills" not in plain.text.splitlines()[0]


def test_bulk_select_all_csv_follows_screen_order(store):
    a, b, c = three(store)
    client = local_client(store)
    shown = [r.get("name") for r in store.all_candidates()]
    page = client.get("/candidates?sort=name&dir=desc").text
    r = client.post("/candidates/bulk", data={"action": "export", "select_all": "1", "f_sort": "name", "f_dir": "desc"})
    names = [x["Name"] for x in csv_rows(r.text)]
    assert names == sorted(shown, key=str.lower, reverse=True) and names != sorted(shown)
    assert page


def test_page_csv_route_has_matched_skills(store):
    three(store)
    client = local_client(store)
    r = client.get("/candidates/export.csv?hl=python:red")
    rows = csv_rows(r.text)
    assert "Matched skills" in rows[0] and any(x["Matched skills"] == "python (1 of 1)" for x in rows)
    assert "Matched skills" not in client.get("/candidates/export.csv").text.splitlines()[0]


def test_roundtrip_import_ignores_matched_skills_column(store):
    three(store)
    text = xlsx_export.csv_in_order(store, [1, 2], [("python", "red")]).decode("utf-8-sig")
    head, body = importers.read_csv(text)
    mapping = importers.guess_mapping(head) if hasattr(importers, "guess_mapping") else None
    assert "Matched skills" in head and body
    if mapping:
        assert mapping[head.index("Matched skills")] != "tags"


def test_xlsx_drops_columns_empty_for_everyone(store):
    a, b, c = three(store)
    client = local_client(store)
    for r in (client.get("/candidates/export.xlsx"),
              client.post("/candidates/bulk", data={"action": "export_xlsx", "candidate_ids": [a, b]})):
        head = read_xlsx(r.content)["Candidates"]["rows"][0]
        assert head[0] == "Name" and "Skills" in head and "Email" in head
        for gone in ("Phone", "Website", "Other links", "Company", "Job", "Stage"):
            assert gone not in head


def test_status_line_wording_and_order_note():
    js = open("src/talent/static/app.js", encoding="utf-8").read()
    assert "1 person has it." in js and "people have it." in js and "have it and are" not in js
    assert "People with more of your picks come first." in js


def test_remove_from_bar_focuses_next_then_previous_then_table(store):
    three(store)
    client = local_client(store)
    html = client.get("/candidates?hl=python:red,sql:blue,rust:green").text
    hrefs = re.findall(r'<a class="chip hl removable" id="(hlbar-[^"]+)"[^>]*href="([^"]+)"', html)
    assert len(hrefs) == 3
    ids = [h[0] for h in hrefs]
    assert hrefs[0][1].endswith("#removed-python@" + ids[1])  # first goes to the next one
    assert hrefs[1][1].endswith("#removed-sql@" + ids[2])
    assert hrefs[2][1].endswith("#removed-rust@" + ids[1])  # last goes to the one before
    solo = re.findall(r'class="chip hl removable"[^>]*href="([^"]+)"', client.get("/candidates?hl=python:red").text)
    assert solo[0].endswith("#removed-python")  # nothing left: the page puts focus on the table
    assert 'id="cand-table"' in html and 'tabindex="-1"' in html


def test_skip_link_and_caption(store):
    three(store)
    client = local_client(store)
    html = client.get("/candidates?hl=python:red").text
    skips = re.findall(r'class="skip-link" href="([^"]+)">([^<]+)<', html)
    assert skips[0] == ("#main", "Skip to main content") and skips[1] == ("#hl-bar", "Skip to highlighted skills")
    assert "Candidates, sorted by highlighted skills first, then by label color" in html
    plain = client.get("/candidates").text
    assert re.findall(r'class="skip-link" href="([^"]+)">([^<]+)<', plain)[1] == ("#cand-table", "Skip to the table")
    assert "highlighted skills first" not in re.search(r"<caption.*?</caption>", plain, re.S).group()


def test_import_drops_formula_looking_skills():
    warnings: list[str] = []
    out = importers._clean_skills(["@cmd", "=SUM(a)", "+x1", "-net", "python", "c++"], "Skills", warnings)
    assert out == ["python", "c++"] and len(warnings) == 4
    assert all("left" in w and "Skills" in w for w in warnings)
