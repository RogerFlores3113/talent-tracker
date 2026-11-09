"""Cycle 10, fixer L (Lena, 5,000 people from an old ATS): changed emails on re-import, bulk merge of duplicate
pairs with one backup, Today and Jobs speed, and an Excel-friendly export that keeps stage dates."""
import csv
import io
import re
import time

import pytest

from talent import actions, duplicates, importers, paths
from talent.store import AIForbidden

from conftest import local_client

HEADER = ["Full Name", "Email", "Company", "Job Title", "Applied For", "Pipeline Stage"]


def _run(store, rows, header=HEADER, **kw):
    mapping = importers.guess_mapping(header, rows)
    return importers.import_rows(store, header, rows, mapping, backup=False, **kw)


def _preview(store, rows, header=HEADER, **kw):
    return importers.preview_import(store, header, rows, importers.guess_mapping(header, rows), **kw)


def _week1(store):
    _run(store, [["Ana Ruiz", "ana@old.com", "Acme", "Engineer", "Backend Dev", "Screening"],
                 ["Bo Lee", "bo@old.com", "Acme", "Designer", "Product Designer", "Sourced"],
                 ["Cy Dan", "cy@old.com", "Zed", "Analyst", "", ""]])


# ------------------------------------------------------------- 1. changed emails
def test_changed_email_with_same_job_updates_the_person_by_default(store):
    _week1(store)
    r = _run(store, [["Ana Ruiz", "ana@new.com", "Acme", "Engineer", "Backend Dev", "Screening"]])
    assert r["added"] == 0 and r["updated"] == 1
    assert store.count_candidates() == 3
    ana = store.find_duplicate("ana@new.com")
    assert ana["name"] == "Ana Ruiz" and ana["email"] == "ana@new.com"
    assert "ana@old.com" in store.other_emails(ana["id"])  # nothing is lost
    [c] = r["email_changes"]
    assert c["strong"] and c["action"] == "update" and c["old_email"] == "ana@old.com"


def test_changed_email_without_the_job_is_flagged_and_added_as_new_by_default(store):
    _week1(store)
    r = _run(store, [["Cy Dan", "cy@new.com", "Zed", "", "", ""]])
    assert r["added"] == 1 and store.count_candidates() == 4
    [c] = r["email_changes"]
    assert not c["strong"] and c["action"] == "new"
    assert any("Cy Dan" in line and "already here" in line for line in r["possible_duplicates"])  # never silent


def test_preview_says_how_many_look_like_existing_people(store):
    _week1(store)
    rows = [["Ana Ruiz", "ana@new.com", "Acme", "Engineer", "Backend Dev", "Screening"],
            ["Bo Lee", "bo@new.com", "Acme", "", "", ""],
            ["Dee New", "dee@new.com", "Acme", "Engineer", "Backend Dev", "Sourced"]]
    p = _preview(store, rows)
    assert len(p["email_changes"]) == 2
    line = importers.preview_lines(p)[1]
    assert line.startswith("2 people look like existing people with a new email")
    assert "1 will be updated" in line and "1 will be added as new people" in line
    assert p["email_change_lines"] and store.count_candidates() == 3  # a preview changes nothing


def test_per_row_and_bulk_choices(store):
    _week1(store)
    rows = [["Ana Ruiz", "ana@new.com", "Acme", "Engineer", "Backend Dev", "Screening"],
            ["Bo Lee", "bo@new.com", "Acme", "Designer", "Product Designer", "Sourced"]]
    p = _preview(store, rows, email_default="new")
    assert [c["action"] for c in p["email_changes"]] == ["new", "new"]
    p = _preview(store, rows, email_default="new", email_choices={2: "update"})  # row 2 is Ana's
    assert [c["action"] for c in p["email_changes"]] == ["update", "new"]
    r = _run(store, rows, email_default="new", email_choices={2: "update"})
    assert r["updated"] == 1 and r["added"] == 1 and store.count_candidates() == 4


def test_two_rows_of_one_file_are_never_each_others_changed_email(store):
    r = _run(store, [["Sam Kim", "sam1@x.com", "Acme", "Engineer", "Dev", "Sourced"],
                     ["Sam Kim", "sam2@x.com", "Acme", "Engineer", "Dev", "Sourced"]])
    assert r["added"] == 2 and not r["email_changes"]


def test_a_different_phone_is_a_different_person(store):
    header = [*HEADER, "Phone"]
    _run(store, [["Ana Ruiz", "ana@old.com", "Acme", "Engineer", "Dev", "Sourced", "555 010 1111"]], header)
    r = _run(store, [["Ana Ruiz", "ana@new.com", "Acme", "Engineer", "Dev", "Sourced", "555 010 2222"]], header)
    assert r["added"] == 1 and not r["email_changes"]


def test_two_saved_people_fit_so_it_is_added_and_flagged_not_guessed(store):
    store.add_candidate(name="Ana Ruiz", email="a1@x.com", current_company="Acme")
    store.add_candidate(name="Ana Ruiz", email="a2@x.com", current_company="Acme")
    r = _run(store, [["Ana Ruiz", "a3@x.com", "Acme", "", "", ""]])
    assert r["added"] == 1 and r["email_changes"][0]["ambiguous"]


def test_web_preview_lists_changed_emails_and_the_run_honors_the_choice(store):
    _week1(store)
    c = local_client(store)
    rows = [HEADER, ["Ana Ruiz", "ana@new.com", "Acme", "Engineer", "Backend Dev", "Screening"],
            ["Bo Lee", "bo@new.com", "Acme", "Designer", "Product Designer", "Sourced"]]
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    r = c.post("/import", files={"file": ("w2.csv", buf.getvalue().encode())})
    assert "2 people look like existing people with a new email" in r.text
    assert "Update this person (add the new email)" in r.text and "Add as new person" in r.text
    token = re.search(r'name="token" value="(\w+)"', r.text).group(1)
    form = {"token": token, "filename": "w2.csv", "source": "CSV", "add_stages": "1", "move_stages": "1",
            "email_default": "new", "emailchg_2": "update",
            **{f"col{i}": m for i, m in enumerate(importers.guess_mapping(HEADER, rows[1:]))}}
    c.post("/import/run", data=form)
    assert store.count_candidates() == 4
    assert store.find_duplicate("ana@new.com")["id"] == store.find_duplicate("ana@old.com")["id"]
    assert store.find_duplicate("bo@new.com")["id"] != store.find_duplicate("bo@old.com")["id"]


# ------------------------------------------------------------- merging, primary email
def _pair(store, a_email="old@x.com", b_email="new@x.com"):
    a = store.add_candidate(name="Ana Ruiz", email=a_email, current_company="Acme", phone="1")
    b = store.add_candidate(name="Ana Ruiz", email=b_email, current_company="Acme")
    return a, b


def test_merge_lets_you_pick_the_main_email(store):
    a, b = _pair(store)
    cand = store.merge_candidates(a["id"], b["id"], primary_email="new@x.com")
    assert cand["email"] == "new@x.com" and store.other_emails(a["id"]) == ["old@x.com"]
    assert store.find_duplicate("old@x.com")["id"] == a["id"]
    a, b = _pair(store, "o2@x.com", "n2@x.com")
    cand = store.merge_candidates(a["id"], b["id"])
    assert cand["email"] == "o2@x.com"  # unchanged without a pick
    with pytest.raises(ValueError):
        a, b = _pair(store, "o3@x.com", "n3@x.com")
        store.merge_candidates(a["id"], b["id"], primary_email="stranger@x.com")


def test_the_ai_can_never_merge_or_change_a_main_email(store):
    a, b = _pair(store)
    with pytest.raises(AIForbidden):
        store.merge_candidates(a["id"], b["id"], by="ai")
    with pytest.raises(AIForbidden):
        store.set_primary_email(a["id"], "new@x.com", by="ai")


def test_single_merge_page_offers_the_email_choice(store):
    a, b = _pair(store)
    c = local_client(store)
    page = c.get(f"/candidates/{a['id']}/merge/{b['id']}").text
    assert "Which email is their main one?" in page and 'value="new@x.com"' in page
    c.post(f"/candidates/{a['id']}/merge/{b['id']}", data={"primary_email": "new@x.com"})
    assert store.get_candidate(a["id"])["email"] == "new@x.com"


# ------------------------------------------------------------- 2. bulk merge
def _many_pairs(store, n):
    ids = []
    for i in range(n):
        a = store.add_candidate(name=f"Person{i} Name{i}", email=f"p{i}@old.com", current_company=f"Co{i}",
                                phone=f"555{i:04d}")
        b = store.add_candidate(name=f"Person{i} Name{i}", email=f"p{i}@new.com", current_company=f"Co{i}")
        ids.append((a["id"], b["id"]))
    return ids


def _merge_backups():
    return list(paths.backups_dir().glob("before-merge-*.db"))


def test_merge_ticked_pairs_makes_one_backup_and_shows_who_is_kept(store):
    ids = _many_pairs(store, 4)
    c = local_client(store)
    assert "Merge ticked pairs" in c.get("/candidates/duplicates").text
    pairs = [p for p in duplicates.find_pairs(store)]
    values = [f"{p['keep']}-{p['other']}" for p in pairs]
    review = c.post("/candidates/duplicates/merge-review", data={"pair": values}).text
    assert "Nothing has changed yet" in review and "the more complete one" in review
    assert "every email from both is kept" in review and "One backup is saved for the whole batch" in review
    assert store.count_candidates() == 8 and not _merge_backups()
    resp = c.post("/candidates/duplicates/merge-ticked", data={"pair": values}, follow_redirects=False)
    assert resp.status_code == 303
    assert store.count_candidates() == 4
    assert len(_merge_backups()) == 1  # ONE backup for the batch, not one per pair
    first = store.get_candidate(ids[0][0])
    assert first["email"] == "p0@old.com" and store.other_emails(first["id"]) == ["p0@new.com"]


def test_merge_ticked_honors_keep_and_main_email_and_skips_stale_pairs(store):
    (a, b), = _many_pairs(store, 1)
    c = local_client(store)
    value = f"{a}-{b}"
    c.post("/candidates/duplicates/merge-ticked",
           data={"pair": [value, "9998-9999"], f"keep_{value}": str(b), f"primary_{value}": "p0@old.com"})
    assert store.count_candidates() == 1
    left = store.get_candidate(b)
    assert left["email"] == "p0@old.com" and "p0@new.com" in store.other_emails(b)


def test_merge_ticked_only_merges_pairs_the_finder_shows(store):
    a = store.add_candidate(name="Ana Ruiz", email="a@x.com", current_company="Acme")
    b = store.add_candidate(name="Zed Quinn", email="z@y.com", current_company="Other")
    c = local_client(store)
    c.post("/candidates/duplicates/merge-ticked", data={"pair": [f"{a['id']}-{b['id']}"]})
    assert store.count_candidates() == 2 and not _merge_backups()


def test_not_the_same_person_in_bulk_still_works(store):
    _many_pairs(store, 3)
    c = local_client(store)
    values = [f"{p['keep']}-{p['other']}" for p in duplicates.find_pairs(store)]
    c.post("/candidates/duplicates/dismiss-many", data={"pair": values})
    assert not duplicates.find_pairs(store) and store.count_candidates() == 6


def test_the_ai_server_has_no_merge_tool():
    from talent import mcp_server
    src = open(mcp_server.__file__, encoding="utf-8").read()
    assert "merge_candidates" not in src and "merge_pairs" not in src


# ------------------------------------------------------------- 3. speed
def _big(store, n, jobs=5):
    header = HEADER
    rows = [[f"Person{i} Name{i}", f"p{i}@x.com", f"Co{i % 40}", f"T{i}", f"Job {i % jobs}",
             ["Sourced", "Screening", "Interview", "Offer"][i % 4]] for i in range(n)]
    _run(store, rows, header)


def test_status_digest_asks_for_imported_placements_once_not_once_per_job(store, monkeypatch):
    _big(store, 200, jobs=6)
    calls = {"all": 0, "one": 0}
    real_all, real_one = importers.imported_placements_by_job, importers.imported_placements
    monkeypatch.setattr(importers, "imported_placements_by_job",
                        lambda s: calls.__setitem__("all", calls["all"] + 1) or real_all(s))
    monkeypatch.setattr(importers, "imported_placements",
                        lambda s, j: calls.__setitem__("one", calls["one"] + 1) or real_one(s, j))
    text = actions.status_digest(store)
    assert calls == {"all": 1, "one": 0}
    assert text.count("(imported)") > 0


def test_status_digest_text_is_the_same_with_or_without_the_shared_lookup(store):
    _big(store, 120, jobs=3)
    job = store.list_jobs()[0]
    solo = actions.job_status_text(store, job["id"], dated=False)
    shared = actions.job_status_text(store, job["id"], dated=False,
                                     imported=importers.imported_placements_by_job(store).get(job["id"], set()))
    assert solo == shared and "(imported)" in solo


def test_today_and_jobs_stay_fast_with_a_big_generated_dataset(store):
    _big(store, 1500, jobs=4)
    c = local_client(store)
    for path in ("/", "/jobs"):
        c.get(path)  # warm
        t = time.perf_counter()
        r = c.get(path)
        took = time.perf_counter() - t
        assert r.status_code == 200
        assert took < 3.0, f"{path} took {took:.2f}s"  # about 0.1s here; the bound is generous for slow CI


# ------------------------------------------------------------- 4. export
def test_export_starts_with_a_byte_order_mark_so_excel_reads_accents(store):
    store.add_candidate(name="Søren Łukasz", email="s@x.com")
    data = importers.export_csv_bytes(store)
    assert data.startswith(b"\xef\xbb\xbfName,")
    r = local_client(store).get("/export.csv")
    assert r.content.startswith(b"\xef\xbb\xbf") and "Søren Łukasz" in r.content.decode("utf-8-sig")


def test_import_accepts_a_file_with_or_without_the_mark(store):
    body = "Name,Email\nSøren Łukasz,s@x.com\n"
    for data in (body.encode("utf-8"), b"\xef\xbb\xbf" + body.encode("utf-8")):
        header, rows = importers.read_csv(importers.decode(data))
        assert header == ["Name", "Email"] and rows[0][0] == "Søren Łukasz"


def test_export_then_import_keeps_stage_dates(store, tmp_path):
    from talent.store import Store
    ana = store.add_candidate(name="Ana Ruiz", email="ana@x.com")
    job = store.add_job("Engineer", "Acme")
    store.add_to_pipeline(ana["id"], job["id"], stage="Screening")
    when = "2026-03-04T12:00:00+00:00"
    with store._lock, store._conn:
        store._conn.execute("UPDATE applications SET stage_changed_at = ? WHERE candidate_id = ?", (when, ana["id"]))
    data = importers.export_csv_bytes(store)
    assert b"Stage changed" in data and b"2026-03-04" in data
    other = Store(tmp_path / "other.db")
    try:
        header, rows = importers.read_csv(importers.decode(data))
        mapping = importers.guess_mapping(header, rows)
        assert "stage_dates" in mapping
        r = importers.import_rows(other, header, rows, mapping, backup=False)
        assert "No stage dates were read" not in r["date_note"]
        got = other.candidate_pipelines(other.find_duplicate("ana@x.com")["id"])
        assert got[0]["stage"] == "Screening" and got[0]["stage_changed_at"].startswith("2026-03-04")
    finally:
        other.close()
