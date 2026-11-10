"""c14 D4: volume work. Job table bulk moves, page size, duplicate clusters, import fixes."""
from __future__ import annotations

import re
from html import unescape

from conftest import local_client
from talent import bulkmove


def make(store, n, tags="python"):
    return [store.add_candidate(name=f"Person {i:03d} Test", tags=tags)["id"] for i in range(n)]


def post(client, url, data, htmx=False):
    return client.post(url, data=data, headers={"HX-Request": "true"} if htmx else {}, follow_redirects=False)


# ---------------------------------------------------------------- 1. job table
def test_job_table_has_ticks_bar_and_skills(store):
    ids = make(store, 3, "python, sql")
    job = store.add_job("Data Lead", "Acme")
    for i in ids:
        store.add_to_pipeline(i, job["id"], "Sourced")
    html = local_client(store).get(f"/jobs/{job['id']}?view=table").text
    assert 'id="bulk-move"' in html and html.count('name="candidate_ids" value=') == 3
    assert "data-check-all" in html and "Move ticked" in html
    assert 'data-skill="python"' in html and f"/jobs/{job['id']}?" in html and "hl=python" in html


def test_bulk_move_one_action_and_one_undo(store):
    ids = make(store, 5)
    job = store.add_job("Data Lead", "Acme")
    for i in ids:
        store.add_to_pipeline(i, job["id"], "Sourced")
    c = local_client(store)
    r = post(c, f"/jobs/{job['id']}/move-many", {"candidate_ids": ids[:4], "stage": "Screening"}, htmx=True)
    assert r.status_code == 200 and "Moved 4 people to Screening." in r.text
    assert {p["candidate_name"]: p["stage"] for p in store.pipeline(job["id"])}["Person 000 Test"] == "Screening"
    assert sum(1 for p in store.pipeline(job["id"]) if p["stage"] == "Screening") == 4
    m = re.search(r'/jobs/\d+/move-many/(\d+)/undo', r.text)
    assert m
    u = post(c, m.group(0), {}, htmx=True)
    assert "Undid the move" in u.text and "4 people" in u.text
    assert all(p["stage"] == "Sourced" for p in store.pipeline(job["id"]))
    again = post(c, m.group(0), {}, htmx=True)
    assert "already undone" in again.text


def test_bulk_move_counts_same_missing_and_errors(store):
    ids = make(store, 3)
    job = store.add_job("Data Lead", "Acme")
    store.add_to_pipeline(ids[0], job["id"], "Screening")
    store.add_to_pipeline(ids[1], job["id"], "Sourced")
    c = local_client(store)
    r = post(c, f"/jobs/{job['id']}/move-many", {"candidate_ids": ids, "stage": "Screening"}, htmx=True)
    assert "Moved 1 person to Screening. 1 was already there. 1 was not in this job" in unescape(r.text)
    assert "Tick at least one" in post(c, f"/jobs/{job['id']}/move-many", {"stage": "Screening"}, htmx=True).text
    assert "Pick the stage" in post(c, f"/jobs/{job['id']}/move-many", {"candidate_ids": ids}, htmx=True).text
    bad = post(c, f"/jobs/{job['id']}/move-many", {"candidate_ids": ids, "stage": "Nonsense stage"}, htmx=True)
    assert "Nobody was moved" in bad.text


def test_bulk_move_non_htmx_and_changes_undo(store):
    ids = make(store, 3)
    job = store.add_job("Data Lead", "Acme")
    for i in ids:
        store.add_to_pipeline(i, job["id"], "Sourced")
    c = local_client(store)
    r = post(c, f"/jobs/{job['id']}/move-many", {"candidate_ids": ids, "stage": "Screening"})
    assert r.status_code == 303
    page = c.get("/changes").text
    assert "Moved 3 people to Screening in Data Lead" in page and "/changes/bulk-move/1/undo" in page
    c.post("/changes/bulk-move/1/undo")
    assert all(p["stage"] == "Sourced" for p in store.pipeline(job["id"]))
    assert bulkmove.batches(store)[0]["undoable"] is False


def test_job_table_picks_sort_by_tiers_inside_stage(store):
    a = store.add_candidate(name="Aaa None", tags="java")["id"]
    b = store.add_candidate(name="Bbb One", tags="python")["id"]
    c_ = store.add_candidate(name="Ccc Both", tags="python, sql")["id"]
    job = store.add_job("Data Lead", "Acme")
    for i in (a, b, c_):
        store.add_to_pipeline(i, job["id"], "Sourced")
    html = local_client(store).get(f"/jobs/{job['id']}?view=table&hl=python:red,sql:yellow").text
    body = html[html.index('id="cand-table"'):]
    assert body.index("Ccc Both") < body.index("Bbb One") < body.index("Aaa None")
    assert 'id="hl-bar"' in html and "2 of 2" in html


def test_add_to_job_confirmation_links_to_job(store):
    ids = make(store, 2)
    job = store.add_job("Data Lead", "Acme")
    c = local_client(store)
    r = c.post("/candidates/bulk", data={"action": "job", "job_id": job["id"], "candidate_ids": ids})
    page = r.text if "Added 2 people" in r.text else c.get("/candidates").text
    assert f'<a href="/jobs/{job["id"]}">Open Data Lead</a>' in page


# ---------------------------------------------------------------- 2. page size
def test_page_size_choice_in_url_and_all_cap(store):
    for i in range(130):
        store.add_candidate(name=f"Pat {i:03d} Test")
    c = local_client(store)
    base = c.get("/candidates").text
    assert base.count('name="candidate_ids" value=') == 100 and 'id="page-size"' in base
    two = c.get("/candidates?size=250").text
    assert two.count('name="candidate_ids" value=') == 130
    assert 'name="f_size" value="250"' in two  # "Use all" and the bar carry it
    assert re.search(r'size=250[^"]*" class="on"', two)
    allp = c.get("/candidates?size=all").text
    assert allp.count('name="candidate_ids" value=') == 130 and "Showing all 130" in allp
    assert 'name="f_size"' not in c.get("/candidates").text
    assert c.get("/candidates?size=bogus").status_code == 200


def test_page_size_all_says_cap(store, monkeypatch):
    import talent.web as web
    monkeypatch.setattr(web, "BULK_MAX", 120)
    for i in range(130):
        store.add_candidate(name=f"Pat {i:03d} Test")
    html = local_client(store).get("/candidates?size=all").text
    assert html.count('name="candidate_ids" value=') == 120
    assert 'id="page-size-cap"' in html and "All stops at 120" in html


# ---------------------------------------------------------------- 3. duplicate clusters
def kim(store, n, **kw):
    return [store.add_candidate(name="Kim Haddad", current_company="Acme", **kw)["id"] for _ in range(n)]


def test_forty_copies_are_one_cluster_card(store):
    from talent import duplicates
    kim(store, 40)
    found = duplicates.clusters(store)
    assert len(found) == 1 and len(found[0]["ids"]) == 40
    html = local_client(store).get("/candidates/duplicates").text
    assert "Kim Haddad, 40 profiles" in html and html.count('class="panel dup-cluster') == 1
    assert html.count('type="radio"') == 40 and html.count('name="m_1"') == 40


def test_clusters_have_no_pair_ceiling_and_page_by_fifty(store):
    from talent import duplicates
    for g in range(60):
        for _ in range(2):
            store.add_candidate(name=f"Name{g:02d} Last{g:02d}", current_company=f"Co{g:02d}")
    assert len(duplicates.clusters(store)) == 60
    c = local_client(store)
    first = c.get("/candidates/duplicates").text
    assert first.count('class="panel dup-cluster') == 50 and "10 groups left" in first
    assert c.get("/candidates/duplicates?shown=100").text.count('class="panel dup-cluster') == 60


def test_merge_cluster_keeps_chosen_and_skips_unticked(store):
    ids = kim(store, 4)
    c = local_client(store)
    keep, out_ = ids[1], ids[3]
    data = {"cluster": "1", f"keep_{ids[0]}": str(keep), f"m_{ids[0]}": [str(i) for i in ids if i != out_]}
    r = c.post("/candidates/duplicates/merge-clusters", data=data)
    assert "Merged 2 profiles into 1 person" in unescape(r.text)
    names = [store.get_candidate(i)["id"] for i in (keep, out_)]  # both still exist: kept one and the unticked one
    assert names == [keep, out_]
    import pytest
    from talent.store import NotFound
    for gone in (ids[0], ids[2]):
        with pytest.raises(NotFound):
            store.get_candidate(gone)


def test_merge_cluster_only_one_button_and_foreign_ids_ignored(store):
    a = kim(store, 2)
    other = store.add_candidate(name="Zed Unrelated", current_company="Elsewhere")["id"]
    c = local_client(store)
    r = c.post("/candidates/duplicates/merge-clusters",
               data={"only": str(a[0]), f"keep_{a[0]}": str(a[0]), f"m_{a[0]}": [str(a[1]), str(other)]})
    assert "Merged 1 profile into 1 person" in unescape(r.text)
    assert store.get_candidate(other)["name"] == "Zed Unrelated"  # not in the group, never merged
    nothing = c.post("/candidates/duplicates/merge-clusters", data={"cluster": "999"})
    assert "Nothing to merge" in nothing.text


def test_maybe_different_people_card_is_unticked(store):
    a = store.add_candidate(name="Kim Haddad", current_company="Acme", current_title="Data Analyst",
                            location="San Francisco", phone="+1 415 555 0101")["id"]
    b = store.add_candidate(name="Kim Haddad", current_company="Acme", current_title="Python Developer",
                            location="Zurich", phone="+41 44 555 0102")["id"]
    html = local_client(store).get("/candidates/duplicates").text
    assert "Same name, maybe different people" in html
    card = html[html.index('id="cluster-'):]
    assert 'name="cluster" value="%d" aria-label' % min(a, b) in card  # no "checked" on the card box
    assert 'value="%d" aria-label="Merge this group' % min(a, b) in card
    assert 'checked aria-label="Merge this group' not in card
    # the same name with the same city is a plain duplicate
    store.add_candidate(name="Lee Park", current_company="Beta", location="Austin")
    store.add_candidate(name="Lee Park", current_company="Beta", location="Austin")
    html2 = local_client(store).get("/candidates/duplicates").text
    assert html2.count("Same name, maybe different people") == 1


def test_dismiss_whole_group(store):
    from talent import duplicates
    kim(store, 3)
    c = local_client(store)
    c.post("/candidates/duplicates/dismiss-cluster", data={"only": "1"})
    assert duplicates.clusters(store) == []


def test_pairs_view_still_there(store):
    kim(store, 2)
    html = local_client(store).get("/candidates/duplicates?view=pairs").text
    assert "Compare and merge" in html and "Show as groups" in html


# ---------------------------------------------------------------- 4. import
CSV = ("Name,Email,Phone,Skills,Notes\n"
       'Ann One,ann@x.com,"\'-555",python,"line one\nline two"\n'
       "Bo Two,bo@x.com,-,python,\n"
       "Cy Three,cy@x.com,+1 415 555 0100,python,\n"
       ",nobody@x.com,,python,\n")


def upload(c, text=CSV):
    r = c.post("/import", files={"file": ("people.csv", text.encode(), "text/csv")})
    return re.search(r'name="token" value="(\w+)"', r.text).group(1), r


def test_phone_without_digits_is_dropped_with_warning(store):
    from talent import importers
    header, rows = importers.read_csv(CSV)
    res = importers.import_rows(store, header, rows, ["name", "email", "phone", "tags", "notes"], backup=False)
    assert store.find_duplicate(email="ann@x.com")["phone"] in (None, "")
    assert store.find_duplicate(email="bo@x.com")["phone"] in (None, "")
    assert store.find_duplicate(email="cy@x.com")["phone"] == "+1 415 555 0100"
    assert any("needs at least 4 digits" in w and "-555" in w for w in res["warnings"])


def test_import_page_offers_skipped_csv_with_reasons_and_lines(store):
    c = local_client(store)
    token, first = upload(c)
    assert "5 lines but 4 rows" in first.text or "lines but 4 rows" in first.text  # a row spans two lines
    assert "Download skipped and blank-cell rows" in first.text
    form = {"token": token, "filename": "people.csv", "source": "CSV import", "col0": "name", "col1": "email",
            "col2": "phone", "col3": "tags", "col4": "note"}
    done = c.post("/import/run", data=form)
    assert "Download skipped and blank-cell rows" in done.text and "lines but 4 rows" in done.text
    csv_text = c.get(f"/import/{token}/result.csv?panel=skipped_blank").text
    lines = csv_text.strip().splitlines()
    assert lines[0] == "Row,Lines in the file,What happened,Reason"
    assert any("Skipped" in x and "no name" in x for x in lines)
    assert any("Saved, a cell left blank" in x and "at least 4 digits" in x for x in lines)
    assert c.get(f"/import/{token}/result.csv?panel=nope").status_code == 404
