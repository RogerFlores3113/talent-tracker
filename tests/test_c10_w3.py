"""Cycle 10, fixer W3: bulk actions on the Candidates table, better changed-email matching on re-import with a way to
merge afterwards, a duplicate finder with a reason filter and safer bulk merge, tidy names, and no repeated
"Other emails" notes."""
import csv
import io
import re

from talent import duplicates, importers
from talent import store as store_mod
from talent.store import Store

from conftest import flash_of, local_client

HEADER = ["Full Name", "Email", "Company", "Phone", "Job Title", "Applied For", "Pipeline Stage"]


def _run(store, rows, header=HEADER, **kw):
    mapping = importers.guess_mapping(header, rows)
    return importers.import_rows(store, header, rows, mapping, backup=False, **kw)


def _preview(store, rows, header=HEADER, **kw):
    return importers.preview_import(store, header, rows, importers.guess_mapping(header, rows), **kw)


def _ids(store):
    return {c["name"]: c["id"] for c in store.all_candidates()}


# ------------------------------------------------------------------ 1. bulk actions
def _people(store, n=5):
    return [store.add_candidate(name=f"Person {i}", email=f"p{i}@x.com", current_company="Acme")["id"] for i in range(n)]


def test_candidates_table_has_row_checkboxes_and_a_bulk_bar(store):
    ids = _people(store, 3)
    html = local_client(store).get("/candidates").text
    for i in ids:
        assert f'name="candidate_ids" value="{i}"' in html
    assert 'id="bulk-form"' in html and 'data-check-name="candidate_ids"' in html
    assert 'value="export"' in html and 'value="color"' in html and 'value="job"' in html
    assert "delete" not in re.search(r'<form id="bulk-form".*?</form>', html, re.S).group(0).lower()


def test_bulk_color_sets_it_for_the_ticked_people_only(store):
    a, b, c, *_ = _people(store)
    r = local_client(store).post("/candidates/bulk", data={"candidate_ids": [a, b], "action": "color", "color": "red"},
                                 follow_redirects=False)
    assert "Set the color to red for 2 people" in flash_of(r)
    assert store.get_candidate(a)["color"] == "red" and store.get_candidate(b)["color"] == "red"
    assert not store.get_candidate(c)["color"]
    r = local_client(store).post("/candidates/bulk", data={"candidate_ids": [a], "action": "color", "color": ""},
                                 follow_redirects=False)
    assert "Cleared the color for 1 person" in flash_of(r) and not store.get_candidate(a)["color"]


def test_bulk_needs_somebody_ticked_and_a_known_action(store):
    a, *_ = _people(store)
    c = local_client(store)
    r = c.post("/candidates/bulk", data={"action": "color", "color": "red"}, follow_redirects=False)
    assert "Tick at least one person" in flash_of(r, "flash_error")
    r = c.post("/candidates/bulk", data={"candidate_ids": [a], "action": "delete"}, follow_redirects=False)
    assert "Pick what to do" in flash_of(r, "flash_error")
    assert store.get_candidate(a)  # there is no bulk delete


def test_bulk_add_to_job_with_a_stage_and_it_leaves_people_already_there(store):
    a, b, c, *_ = _people(store)
    job = store.add_job(title="Backend Dev", company="Acme")["id"]
    store.add_to_pipeline(c, job, "Screening")
    stages = [s["name"] for s in store.job_stages(job)]
    stage = stages[1]
    r = local_client(store).post("/candidates/bulk", data={"candidate_ids": [a, b, c], "action": "job", "job_id": job,
                                                           "stage": stage.lower()}, follow_redirects=False)
    notice = flash_of(r)
    assert "Added 2 people to Backend Dev" in notice and "1 was already in it" in notice
    assert store.get_application(store._row("SELECT id FROM applications WHERE candidate_id = ?", (a,))["id"])["stage"] == stage
    row = store._row("SELECT stage FROM applications WHERE candidate_id = ? AND job_id = ?", (c, job))
    assert row["stage"] == "Screening"  # left where they were


def test_bulk_add_to_job_refuses_an_unknown_stage_and_adds_nobody(store):
    a, *_ = _people(store)
    job = store.add_job(title="Backend Dev", company="Acme")["id"]
    r = local_client(store).post("/candidates/bulk", data={"candidate_ids": [a], "action": "job", "job_id": job,
                                                           "stage": "Nonsense"}, follow_redirects=False)
    assert "Nobody was added" in flash_of(r, "flash_error")
    assert not store._row("SELECT 1 AS x FROM applications WHERE candidate_id = ?", (a,))


def test_bulk_export_is_only_the_ticked_people(store):
    a, b, c, *_ = _people(store)
    r = local_client(store).post("/candidates/bulk", data={"candidate_ids": [a, c], "action": "export"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    names = {row[0] for row in rows[1:]}
    assert names == {"Person 0", "Person 2"}


def test_use_all_results_covers_every_match_not_just_the_page(store):
    for i in range(130):  # more than the 100 a page shows
        store.add_candidate(name=f"Dev {i}", email=f"d{i}@x.com", current_title="Developer")
    other = store.add_candidate(name="Chef Ann", email="ann@x.com", current_title="Chef")["id"]
    c = local_client(store)
    r = c.post("/candidates/bulk", data={"select_all": "1", "action": "color", "color": "blue", "f_q": "developer"},
               follow_redirects=False)
    assert "Set the color to blue for 130 people" in flash_of(r)
    assert not store.get_candidate(other)["color"]
    assert store.color_counts()["blue"] == 130
    page = c.get("/candidates?q=developer").text
    assert 'name="select_all"' in page and "Use all 130 people in these results" in page


def test_use_all_results_follows_the_filters_without_a_search(store):
    a = store.add_candidate(name="A One", email="a@x.com", location="Austin")["id"]
    store.add_candidate(name="B Two", email="b@x.com", location="Denver")
    r = local_client(store).post("/candidates/bulk", data={"select_all": "1", "action": "color", "color": "green",
                                                           "f_location": "Austin"}, follow_redirects=False)
    assert "for 1 person" in flash_of(r) and store.get_candidate(a)["color"] == "green"


def test_the_bulk_actions_are_not_ai_tools():
    from talent import mcp_server
    src = open(mcp_server.__file__, encoding="utf-8").read()
    assert "candidates/bulk" not in src and "set_color_many" not in src


# ------------------------------------------------------------------ 2. changed emails on re-import
def _week1(store):
    _run(store, [["Ana Ruiz", "ana@old.com", "Acme", "", "Engineer", "Backend Dev", "Screening"],
                 ["Sam Kim", "sam1@x.com", "Acme", "555 010 1111", "", "", ""],
                 ["Sam Kim", "sam2@x.com", "Acme", "555 010 2222", "", "", ""],
                 ["Joe Lee", "joe@old.com", "Zed", "", "", "", ""],
                 ["Joe Lee", "joe2@old.com", "Zed", "", "Dev", "Backend Dev", "Screening"]])


def test_same_name_and_company_is_a_strong_match_and_updates_by_default(store):
    _week1(store)
    r = _run(store, [["Ana Ruiz", "ana@new.com", "Acme", "", "", "", ""]])  # no job in the row
    assert r["added"] == 0 and r["updated"] == 1
    [c] = r["email_changes"]
    assert c["strong"] and c["action"] == "update" and "company" in c["why"]
    ana = store.find_duplicate("ana@new.com")
    assert ana["email"] == "ana@new.com" and "ana@old.com" in store.other_emails(ana["id"])


def test_same_name_and_same_phone_without_a_company_is_a_strong_match(store):
    _week1(store)
    r = _run(store, [["Sam Kim", "sam.new@x.com", "", "555-010-1111", "", "", ""]])
    [c] = r["email_changes"]
    assert c["strong"] and c["action"] == "update" and "phone" in c["why"] and r["added"] == 0
    assert store.find_duplicate("sam.new@x.com")["email"] == "sam.new@x.com"
    assert "sam1@x.com" in store.other_emails(store.find_duplicate("sam.new@x.com")["id"])


def test_a_namesake_with_a_different_phone_is_never_matched(store):
    _week1(store)
    r = _run(store, [["Sam Kim", "sam3@x.com", "Other Co", "555 010 9999", "", "", ""]])
    assert r["added"] == 1 and not r["email_changes"]


def test_several_saved_people_fit_the_same_phone_picks_one(store):
    _week1(store)  # two Sam Kim at Acme, with different phones
    r = _run(store, [["Sam Kim", "sam.new@x.com", "Acme", "555 010 2222", "", "", ""]])
    [c] = r["email_changes"]
    assert c["strong"] and c["action"] == "update" and c["old_email"] == "sam2@x.com" and r["added"] == 0


def test_several_fit_the_job_picks_the_one_on_it(store):
    _week1(store)  # two Joe Lee at Zed, only joe2 is on Backend Dev
    r = _run(store, [["Joe Lee", "joe.new@x.com", "Zed", "", "Dev", "Backend Dev", "Screening"]])
    [c] = r["email_changes"]
    assert c["strong"] and c["old_email"] == "joe2@old.com" and c["action"] == "update"


def test_several_fit_and_nothing_tells_them_apart_lists_the_candidates_and_adds_as_new(store):
    _week1(store)
    r = _run(store, [["Sam Kim", "sam.new@x.com", "Acme", "", "", "", ""]])
    [c] = r["email_changes"]
    assert not c["strong"] and c["ambiguous"] and c["action"] == "new" and c["new_id"]
    assert {k["email"] for k in c["candidates"]} == {"sam1@x.com", "sam2@x.com"}


def test_the_recruiter_can_pick_one_of_several(store):
    _week1(store)
    sam2 = store.find_duplicate("sam2@x.com")["id"]
    r = _run(store, [["Sam Kim", "sam.new@x.com", "Acme", "", "", "", ""]], email_choices={2: f"update:{sam2}"})
    [c] = r["email_changes"]
    assert c["action"] == "update" and c["id"] == sam2 and r["added"] == 0
    assert store.get_candidate(sam2)["email"] == "sam.new@x.com"
    p = _preview(store, [["Sam Kim", "sam.x@x.com", "Acme", "", "", "", ""]])
    assert p["email_changes"][0]["action"] == "new"  # unchanged default: never guessed


def test_import_page_offers_candidates_and_the_table_header_cells_have_scope(store):
    _week1(store)
    buf = io.StringIO()
    csv.writer(buf).writerows([HEADER, ["Sam Kim", "sam.new@x.com", "Acme", "", "", "", ""]])
    c = local_client(store)
    html = c.post("/import", files={"file": ("w2.csv", buf.getvalue().encode())}).text
    assert 'name="emailchg_2"' in html and "Update sam1@x.com" in html and "Update sam2@x.com" in html
    table = re.search(r'<fieldset class="panel" id="email-changes".*?</fieldset>', html, re.S).group(0)
    assert table.count("<th scope=\"col\">") == 4 and "<th>" not in table


def test_result_page_lists_new_people_with_a_likely_match_and_a_merge_button(store):
    _week1(store)
    buf = io.StringIO()
    csv.writer(buf).writerows([HEADER, ["Sam Kim", "sam.new@x.com", "Acme", "", "", "", ""]])
    c = local_client(store)
    up = c.post("/import", files={"file": ("w2.csv", buf.getvalue().encode())}).text
    token = re.search(r'name="token" value="(\w+)"', up).group(1)
    form = {"token": token, "filename": "w2.csv", "source": "CSV import"}
    for i, t in enumerate(["name", "email", "company", "phone", "job_title", "job_company", "stage"]):
        form[f"col{i}"] = importers.guess_mapping(HEADER, [])[i] if False else t
    mapping = importers.guess_mapping(HEADER, [["x"] * 7])
    form.update({f"col{i}": m for i, m in enumerate(mapping)})
    res = c.post("/import/run", data=form)
    assert 'id="came-in-as-new"' in res.text and "Came in as new" in res.text
    assert "circular" not in res.text and "import again and choose Update" not in res.text
    new_id = store.find_duplicate("sam.new@x.com")["id"]
    assert f'/candidates/{store.find_duplicate("sam1@x.com")["id"]}/merge/{new_id}' in res.text
    assert "Merge with sam1@x.com" in res.text and "Merge with sam2@x.com" in res.text


def test_merge_button_goes_through_the_normal_merge_with_a_backup(store):
    _week1(store)
    r = _run(store, [["Ana Ruiz", "ana@new.com", "Acme", "", "", "", ""]], email_default="new")
    [ch] = r["email_changes"]
    old, new = ch["id"], ch["new_id"]
    c = local_client(store)
    res = c.post(f"/candidates/{old}/merge/{new}", data={"primary_email": "ana@new.com"},
                 headers={"HX-Request": "true"})
    assert res.status_code == 200 and "Merged" in res.text and "A backup was saved first" in res.text
    assert store._row("SELECT 1 AS x FROM candidates WHERE id = ?", (new,)) is None
    kept = store.get_candidate(old)
    assert kept["email"] == "ana@new.com" and "ana@old.com" in store.other_emails(old)


# ------------------------------------------------------------------ 3. duplicate finder
def _pairs(store):
    store.add_candidate(name="Kim Lo", email="kim@a.com", current_company="Acme")
    store.add_candidate(name="Kim Lo", email="kim@b.com", current_company="Acme")      # same name and company
    store.add_candidate(name="Dee Fox", email="dee@a.com", phone="415 555 0100")
    store.add_candidate(name="Dee Fox", email="dee@b.com", phone="415-555-0100")        # same name and phone


def test_reason_filter_lists_only_that_reason_and_counts(store):
    _pairs(store)
    c = local_client(store)
    everything = c.get("/candidates/duplicates").text
    assert "Same name and phone (1)" in everything and "Same name and company, different emails (1)" in everything
    one = c.get("/candidates/duplicates?reason=name-phone").text
    assert "dee@a.com" in one and "kim@a.com" not in one
    assert 'aria-current="true">Same name and phone (1)' in one
    assert duplicates.reason_slug("bogus") is None
    assert c.get("/candidates/duplicates?reason=bogus").status_code == 200


def test_every_pair_of_a_filter_is_covered_across_pages(store):
    for i in range(60):
        store.add_candidate(name=f"Twin {i}", email=f"t{i}@a.com", current_company="Acme")
        store.add_candidate(name=f"Twin {i}", email=f"t{i}@b.com", current_company="Acme")
    c = local_client(store)
    page = c.get("/candidates/duplicates?reason=name-company-emails").text
    assert "Use all 60 pairs" in page and 'name="all_filtered"' in page
    res = c.post("/candidates/duplicates/merge-review", data={"all_filtered": "1", "reason": "name-company-emails"})
    assert res.status_code == 200 and len(re.findall(r'<input type="checkbox" name="pair"', res.text)) == 60  # not just the 50 on the page
    assert 'name="pair" value="' in res.text and not re.search(r'name="pair"[^>]*checked', res.text)


def test_other_filter_pairs_are_not_pulled_in_by_all_filtered(store):
    _pairs(store)
    c = local_client(store)
    res = c.post("/candidates/duplicates/merge-review", data={"all_filtered": "1", "reason": "name-phone"})
    assert len(re.findall(r'<input type="checkbox" name="pair"', res.text)) == 1 and "dee@a.com" in res.text and "kim@a.com" not in res.text


def test_bulk_merge_pages_start_unticked(store):
    _pairs(store)
    store.add_candidate(name="Gus Day", email="gus@gmail.com")
    store.add_candidate(name="Gus Day", email="g.us+jobs@gmail.com")
    c = local_client(store)
    review = c.post("/candidates/duplicates/merge-review", data={"all_filtered": "1"}).text
    assert 'name="pair"' in review and "checked" not in review
    exact = c.get("/candidates/duplicates/merge-exact").text
    assert 'name="pair"' in exact and "checked" not in exact
    assert "sr-only\">Pairs: " in review  # the heading says what the number is


def test_nothing_is_merged_when_nothing_is_ticked_on_the_review_page(store):
    _pairs(store)
    c = local_client(store)
    n = store.count_candidates()
    r = c.post("/candidates/duplicates/merge-ticked", data={}, follow_redirects=False)
    assert "No pairs were ticked" in flash_of(r, "flash_error") and store.count_candidates() == n


def test_newer_email_is_the_default_main_email(store):
    old = store.add_candidate(name="Sofia Johansson", email="sofia.old@a.com", current_company="Acme",
                              phone="1", about="x", location="Oslo")["id"]
    new = store.add_candidate(name="Sofia Johansson", email="new_sofia@b.com", current_company="Acme")["id"]
    a, b = store.get_candidate(old), store.get_candidate(new)
    assert duplicates.newer_email(a, b) == "new_sofia@b.com"
    c = local_client(store)
    review = c.post("/candidates/duplicates/merge-review", data={"pair": f"{old}-{new}"}).text
    sel = re.search(r'<select name="primary_[^"]*".*?</select>', review, re.S).group(0)
    assert re.search(r'<option value="new_sofia@b.com" selected', sel) and "sofia.old@a.com\" selected" not in sel
    page = c.get(f"/candidates/{old}/merge/{new}").text
    assert re.search(r'value="new_sofia@b.com" checked', page)
    res = c.post("/candidates/duplicates/merge-ticked", data={"pair": f"{old}-{new}",
                                                              f"keep_{old}-{new}": str(old),
                                                              f"primary_{old}-{new}": "new_sofia@b.com"},
                 follow_redirects=False)
    assert "Merged 1 pair" in flash_of(res)
    assert store.get_candidate(old)["email"] == "new_sofia@b.com"


def test_same_name_pairs_are_labelled_with_email_or_company(store):
    a = store.add_candidate(name="Sam Dup", email="sam@x.com", current_company="Acme")
    b = store.add_candidate(name="Sam Dup", email="sam2@y.com", current_company="Beta")
    assert duplicates.pair_labels(a, b) == ("Sam Dup (sam@x.com)", "Sam Dup (sam2@y.com)")
    c1 = store.add_candidate(name="Ann Co", current_company="Acme")
    c2 = store.add_candidate(name="Ann Co", current_company="Beta")
    assert duplicates.pair_labels(c1, c2) == ("Ann Co (Acme)", "Ann Co (Beta)")
    c3 = store.add_candidate(name="Zed Same")
    c4 = store.add_candidate(name="Zed Same")
    assert duplicates.pair_labels(c3, c4)[0].startswith("Zed Same (id ")
    d1 = store.add_candidate(name="Bo One", email="b@x.com")
    d2 = store.add_candidate(name="Bo Won", email="c@x.com")
    assert duplicates.pair_labels(d1, d2) == ("Bo One", "Bo Won")  # different names need nothing added


def test_review_and_single_merge_names_tell_the_two_apart(store):
    a = store.add_candidate(name="Sam Dup", email="sam@x.com", current_company="Acme", phone="1")["id"]
    b = store.add_candidate(name="Sam Dup", email="sam2@y.com", current_company="Acme")["id"]
    c = local_client(store)
    review = c.post("/candidates/duplicates/merge-review", data={"pair": f"{a}-{b}"}).text
    assert 'aria-label="Merge Sam Dup (sam@x.com) and Sam Dup (sam2@y.com)"' in review
    assert 'aria-label="Which profile is kept: Sam Dup (sam@x.com) or Sam Dup (sam2@y.com)"' in review
    assert "Sam Dup (id " not in review
    assert 'aria-label="Main email for Sam Dup (sam@x.com) and Sam Dup (sam2@y.com)"' in review
    page = c.get(f"/candidates/{a}/merge/{b}").text
    assert "Merge into Sam Dup (sam@x.com)" in page and "Keep Sam Dup (sam2@y.com) instead" in page
    lst = c.get("/candidates/duplicates").text
    assert 'aria-label="Pick the pair Sam Dup (sam@x.com) and Sam Dup (sam2@y.com)"' in lst
    assert 'aria-label="Compare and merge Sam Dup (sam@x.com) and Sam Dup (sam2@y.com)"' in lst


# ------------------------------------------------------------------ 4. tidy names
def test_tidy_name_rules():
    assert importers.tidy_name("  Priya   NGUYEN ") == "Priya NGUYEN"  # mixed case stays, spaces collapse
    assert importers.tidy_name("PRIYA NGUYEN") == "Priya Nguyen"
    assert importers.tidy_name("priya nguyen") == "Priya Nguyen"
    assert importers.tidy_name("MARY-JANE O'BRIEN") == "Mary-Jane O'Brien"
    assert importers.tidy_name("McDonald") == "McDonald"
    assert importers.tidy_name("Ana de la Cruz") == "Ana de la Cruz"
    assert importers.tidy_name("Ana  de   la Cruz") == "Ana de la Cruz"  # spaces still collapse
    assert importers.tidy_name("Ron McDONALD") == "Ron McDONALD"  # mixed case is left alone
    assert importers.tidy_name("") == "" and importers.tidy_name("日本語") == "日本語"


def test_import_tidies_names(store):
    header = ["Name", "Email"]
    rows = [["PRIYA NGUYEN", "p@x.com"], ["  wei   chen ", "w@x.com"], ["Rory McDonald", "r@x.com"],
            ["Ana de la Cruz", "a@x.com"]]
    _run(store, rows, header)
    assert sorted(_ids(store)) == ["Ana de la Cruz", "Priya Nguyen", "Rory McDonald", "Wei Chen"]


def test_first_and_last_name_columns_are_tidied_too(store):
    header = ["First Name", "Last Name", "Email"]
    _run(store, [["PRIYA", "NGUYEN", "p@x.com"]], header)
    assert list(_ids(store)) == ["Priya Nguyen"]


# ------------------------------------------------------------------ 5. other emails only in their field
def test_other_emails_are_not_repeated_in_notes(store):
    header = ["Name", "Email", "Emails"]
    r = _run(store, [["Ana Ruiz", "ana@work.com", "ana@work.com; ana@home.org, ana@old.net"]], header[:2] + ["Other Emails"])
    ana = store.find_duplicate("ana@work.com")
    assert store.other_emails(ana["id"]) == ["ana@home.org", "ana@old.net"]
    assert not [t for t in store.timeline(ana["id"]) if "Other emails" in (t.get("body") or "")]
    assert r["notes"] == 0


def test_several_emails_in_one_cell_have_no_note_either(store):
    header = ["Name", "Emails"]
    _run(store, [["Bo Lee", "bo@work.com; bo@home.org"]], header)
    bo = store.find_duplicate("bo@work.com")
    assert store.other_emails(bo["id"]) == ["bo@home.org"]
    assert not [t for t in store.timeline(bo["id"]) if t["kind"] == "note"]


def test_export_does_not_repeat_other_emails_in_notes(store):
    c = store.add_candidate(name="Cy Dan", email="cy@x.com", other_emails=["cy@home.org"])["id"]
    store.log(c, "note", "Other emails: cy@home.org")  # what an older import wrote
    store.log(c, "note", "Likes remote work")
    rows = list(csv.DictReader(io.StringIO(importers.export_csv(store))))
    [row] = rows
    assert row["Other emails"] == "cy@home.org"
    assert "Other emails:" not in row["Notes"] and "Likes remote work" in row["Notes"]


def test_a_reimported_old_export_notes_cell_does_not_bring_the_note_back(store, tmp_path):
    c = store.add_candidate(name="Cy Dan", email="cy@x.com", other_emails=["cy@home.org"])["id"]
    store.log(c, "note", "Likes remote work")
    text = importers.export_csv(store)
    header, rows = importers.read_csv(text.replace("Likes remote work", "Other emails: cy@home.org | Likes remote work"))
    other = Store(tmp_path / "other.db")
    importers.import_rows(other, header, rows, importers.guess_mapping(header, rows), backup=False)
    cy = other.find_duplicate("cy@x.com")
    assert other.other_emails(cy["id"]) == ["cy@home.org"]
    bodies = [t["body"] for t in other.timeline(cy["id"]) if t["kind"] == "note"]
    assert not any("Other emails" in b for b in bodies) and any("remote" in b for b in bodies)
    other.close()


def test_migration_removes_old_notes_that_only_repeat_the_field(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    steps = store_mod._MIGRATIONS
    ours = next(i for i, st in enumerate(steps) if isinstance(st, tuple) and st[1] is store_mod._migrate_v10w3_data)
    monkeypatch.setattr(store_mod, "_MIGRATIONS", steps[:ours])
    s = Store(path)
    a = s.add_candidate(name="Ana", email="a@x.com", other_emails=["a2@x.com"])["id"]
    b = s.add_candidate(name="Bo", email="b@x.com")["id"]
    s.log(a, "note", "Other emails: a2@x.com")
    s.log(a, "note", "Other emails: a2@x.com, someone@else.com")  # an address the field doesn't have: kept
    s.log(a, "note", "Other emails: a2@x.com are the ones to use after 5pm")  # a person's own words: kept
    s.log(b, "note", "Other emails: nobody@x.com")  # not in b's field: kept
    s.close()
    monkeypatch.setattr(store_mod, "_MIGRATIONS", steps)
    s = Store(path)
    bodies = lambda i: {t["body"] for t in s.timeline(i) if t["kind"] == "note"}  # noqa: E731
    assert "Other emails: a2@x.com" not in bodies(a)
    assert len(bodies(a)) == 2 and len(bodies(b)) == 1
    assert s.other_emails(a) == ["a2@x.com"]
    s.close()
