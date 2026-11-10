"""Cycle 16: import result and preview wording, repeat lists, job matching, client report without lost names."""
from __future__ import annotations

import re
from conftest import local_client

from talent import actions, importers, reports


def _csv(n: int, repeats: int = 0) -> str:
    lines = ["Name,Email"] + [f"Person {i},p{i}@x.com" for i in range(n)]
    lines += [f"Person {i},p{i}@x.com" for i in range(repeats)]
    return "\n".join(lines) + "\n"


def _upload(c, text, name="a.csv"):
    r = c.post("/import", files={"file": (name, text.encode(), "text/csv")})
    return r, re.search(r'name="token" value="(\w+)"', r.text).group(1)


def test_repeats_listed_once_and_never_already_here_on_an_empty_database(store):
    c = local_client(store)
    _, token = _upload(c, _csv(60, 5))
    done = c.post("/import/run", data={"token": token, "col0": "name", "col1": "email", "filename": "a.csv"}).text
    assert "65 rows: 60 people added, 5 repeats merged." in done
    assert "Rows saved as one person (5)" in done
    assert "already here" not in done and "Folded into" not in done


def test_headline_uses_thousands_and_only_the_parts_that_apply(store):
    res = {"added": 1960, "repeats": 40, "updated": 0, "unchanged": 0, "skipped_rows": 0, "notes": 0}
    assert importers.result_headline(res, 2000) == "2,000 rows: 1,960 people added, 40 repeats merged."
    res = {"added": 1, "repeats": 0, "updated": 2, "unchanged": 0, "skipped_rows": 1, "notes": 0}
    assert importers.result_headline(res, 4) == "4 rows: 1 person added, 2 updated, 1 row skipped."


def test_reimport_says_nothing_would_change_and_button_is_not_primary(store):
    header, rows = importers.read_csv(_csv(30))
    importers.import_rows(store, header, rows, ["name", "email"], backup=False)
    c = local_client(store)
    r, _ = _upload(c, _csv(30))
    assert "Nothing will change: 30 people are in this file already up to date." in r.text
    assert "Import anyway" in r.text and 'class="btn primary" type="submit">Import' not in r.text
    assert "takes about a minute" not in r.text


def test_stage_paragraph_only_when_a_stage_column_is_mapped(store):
    c = local_client(store)
    r, _ = _upload(c, _csv(3))
    assert "Move people" not in r.text
    r, _ = _upload(c, "Name,Email,Job,Stage\nAnn,a@x.com,Designer,Screening\n")
    assert "Move people to the stage this file shows" in r.text


def test_first_screen_lists_the_columns_and_dropdown_is_grouped(store):
    c = local_client(store)
    first = c.get("/import").text
    assert "Columns we understand" in first and "separated by commas or semicolons" in first
    r, _ = _upload(c, _csv(3))
    assert '<optgroup label="Contact">' in r.text and '<optgroup label="Pipeline">' in r.text
    for t in importers.IMPORT_TARGETS:
        assert f'value="{t}"' in r.text
    for g, ts in importers.target_groups():
        assert ts


def test_job_title_matches_the_only_job_with_that_title(store):
    store.add_job("Product Designer", company="Globex")
    header, rows = importers.read_csv("Name,Email,Job\nAnn,a@x.com,Product Designer\n")
    res = importers.import_rows(store, header, rows, ["name", "email", "job_title"], backup=False)
    assert [j["title"] for j in store.list_jobs()] == ["Product Designer"]
    assert res["pipelines_added"] == 1 and not res["jobs_created"]


def test_job_title_that_fits_several_jobs_is_not_guessed(store):
    store.add_job("Product Designer", company="Globex")
    store.add_job("Product Designer", company="Initech")
    header, rows = importers.read_csv("Name,Email,Job\nAnn,a@x.com,Product Designer\nBo,b@x.com,Product Designer\n")
    mapping = ["name", "email", "job_title"]
    prev = importers.preview_import(store, header, rows, mapping)
    assert any("fits 2 jobs here" in line for line in importers.preview_lines(prev))
    res = importers.import_rows(store, header, rows, mapping, backup=False)
    assert len(res["job_notes"]) == 1 and res["jobs_created"] == ["Product Designer"]


def test_client_report_names_no_one_who_was_rejected_or_withdrew(store):
    job = store.add_job("Backend Engineer", company="Acme", stages=actions.stage_set("agency"))
    ann = store.add_candidate(name="Ann Hired", email="ann@x.com")["id"]
    rob = store.add_candidate(name="Rob Rejected", email="rob@x.com")["id"]
    for cid in (ann, rob):
        store.add_to_pipeline(cid, job["id"], "Submitted")
    store.move_stage(ann, job["id"], "Placed", by="app")
    store.move_stage(rob, job["id"], "Client passed", by="app")
    rep = reports.report(store, "acme")
    out = reports.report_text(rep)
    assert "Client passed" not in out and "Ann Hired to Placed" in out
    assert "- Out: 1" in out
