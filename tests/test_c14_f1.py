"""c14 F1: web fixes from the cycle 14 re-review (HM view, share, skill names, duplicates, import, focus, layout)."""
from __future__ import annotations

import html as htmllib
import re

from conftest import local_client
from test_c13_c import read_xlsx

from talent import duplicates, importers, picks, xlsx_export


def reject(store, cid, title="Senior Backend"):
    job = next((j for j in store.list_jobs() if j["title"] == title), None) or store.add_job(title, "Acme")
    store.add_to_pipeline(cid, job["id"], "Rejected")
    return job["id"]


def three(store):
    sofia = store.add_candidate(name="Sofia Example", current_title="Backend Engineer", tags="java, management")["id"]
    maya = store.add_candidate(name="Maya Rivera", current_title="Backend Engineer", tags="java, python")["id"]
    kenji = store.add_candidate(name="Kenji Mori", current_title="Backend Engineer", tags="java, sql")["id"]
    return sofia, maya, kenji


# ------------------------------------------------------------------ 1. hiring manager view
def test_hm_view_really_leaves_out_a_person_not_in_play(store):
    sofia, maya, kenji = three(store)
    reject(store, sofia)
    c = local_client(store)
    html = c.get(f"/candidates/compare?ids={sofia},{maya},{kenji}&view=hm").text
    table = html[html.index('id="compare-table"'):html.index("</table>")]
    glance = html[html.index('id="glance-h"'):html.index('id="side-h"')]
    assert "Sofia" not in table and "Sofia" not in glance and "management" not in html.split('id="hm-left-out"')[0][-0:] + glance
    assert "2 people" in html and "Side by side: 2 people." in html
    line = re.search(r'id="hm-left-out"[^>]*>([^<]+)<', html).group(1)
    assert "Sofia Example" in line and "left out" in line and "not in play" in line
    # the full comparison still has her
    assert "Sofia" in c.get(f"/candidates/compare?ids={sofia},{maya},{kenji}").text


def test_hm_view_with_fewer_than_two_left_says_so(store):
    sofia, maya, kenji = three(store)
    reject(store, sofia)
    reject(store, maya)
    html = local_client(store).get(f"/candidates/compare?ids={sofia},{maya},{kenji}&view=hm").text
    assert "Fewer than two people are still in play" in html and "Sofia Example" in html


# ------------------------------------------------------------------ 2. share with no job
def test_share_without_a_job_says_who_is_rejected_and_leaves_them_out(store):
    sofia, maya, _ = three(store)
    reject(store, sofia)
    c = local_client(store)
    html = c.get(f"/candidates/share?ids={sofia},{maya}").text
    assert "Sofia Example is marked rejected in Senior Backend" in html
    assert re.search(rf'name="leave_rej" value="{sofia}"[^>]*checked', html)
    assert "Maya Rivera is marked rejected" not in html
    r = c.post("/candidates/share", data={"ids": f"{sofia},{maya}", "leave_rej": str(sofia)})
    names = [row[0] for row in read_xlsx(r.content)["Candidates"]["rows"][1:]]
    assert names == ["Maya Rivera"]
    r = c.post("/candidates/share", data={"ids": f"{sofia},{maya}"})  # unticked: she stays in
    names = [row[0] for row in read_xlsx(r.content)["Candidates"]["rows"][1:]]
    assert "Sofia Example" in names and "Maya Rivera" in names


# ------------------------------------------------------------------ 3. skill names
def react_seed(store):
    for i in range(3):
        store.add_candidate(name=f"R{i}", tags="react")
    for i in range(2):
        store.add_candidate(name=f"J{i}", tags="react.js")
    store.add_candidate(name="X1", tags="reactjs")
    store.add_candidate(name="P1", tags="python")
    store.add_candidate(name="P2", tags="python3")
    store.add_candidate(name="N1", tags="nodejs")
    store.add_candidate(name="N2", tags="node")


def test_three_spellings_are_one_row_and_python3_is_suggested(store):
    react_seed(store)
    rows = store.skill_lookalike_groups()
    byfirst = {tuple(sorted(n.lower() for n in r["names"])): r for r in rows}
    assert ("react", "react.js", "reactjs") in byfirst and byfirst[("react", "react.js", "reactjs")]["keep"] == "react"
    assert ("python", "python3") in byfirst
    assert ("node", "nodejs") in byfirst  # still only a suggestion
    assert store.skill_alias_map() == {}
    html = local_client(store).get("/settings").text
    assert html.count("Treat react, react.js, reactjs as one skill") == 1
    assert "Name to keep for react / react.js / reactjs" in html


def test_after_a_merge_a_pair_shows_as_the_group(store):
    react_seed(store)
    c = local_client(store)
    c.post("/settings/skill-names/merge", data={"a": "react", "b": "react.js", "kept": "react"})
    pairs = store.skill_lookalikes(50)
    shown = {frozenset((p["a"].lower(), p["b"].lower())) for p in pairs}
    assert frozenset(("react", "reactjs")) in shown and frozenset(("react.js", "reactjs")) not in shown
    html = c.get("/settings").text
    assert "react / reactjs" in html and "react.js / reactjs" not in html


def test_group_treat_as_one_and_result_takes_focus(store):
    react_seed(store)
    c = local_client(store)
    r = c.post("/settings/skill-names/merge", data={"names": ["react", "react.js", "reactjs"], "kept": "react"},
               follow_redirects=True)
    assert store.skill_alias_map() == {"react.js": "react", "reactjs": "react"}
    m = re.search(r'<h3[^>]*id="skill-result"[^>]*>([^<]+)</h3>', r.text)
    assert m and "tabindex=\"-1\"" in m.group(0) and "data-focus-here" in m.group(0)
    assert "count as react" in htmllib.unescape(m.group(1))
    r = c.post("/settings/skill-names/split", data={"name": "react"}, follow_redirects=True)
    assert 'id="skill-result"' in r.text and store.skill_alias_map() == {}


def test_not_the_same_is_listed_and_can_be_undone(store):
    react_seed(store)
    c = local_client(store)
    r = c.post("/settings/skill-names/reject", data={"a": "python", "b": "python3"}, follow_redirects=True)
    assert 'id="skill-result"' in r.text
    html = c.get("/settings").text
    assert "Hidden suggestions" in html and "python / python3" in html
    assert "Treat python, python3 as one skill" not in html
    c.post("/settings/skill-names/unreject", data={"a": "python", "b": "python3"})
    assert "Treat python, python3 as one skill" in c.get("/settings").text


def test_merge_two_names_you_type_and_legend_and_label_for(store):
    react_seed(store)
    c = local_client(store)
    r = c.post("/settings/skill-names/merge", data={"kept": "node", "alias": "nodejs"}, follow_redirects=True)
    assert store.skill_alias_map() == {"nodejs": "node"} and 'id="skill-result"' in r.text
    err = c.post("/settings/skill-names/merge", data={"kept": "node", "alias": "node"}, follow_redirects=True)
    assert "already the same" in err.text
    html = c.get("/settings").text
    assert 'id="skill-type-form"' in html
    assert re.search(r'<input type="radio" id="(keep-\d+-\d+)"[^>]*> <label for="\1"', html)


def test_merges_and_splits_are_on_changes_with_undo(store):
    react_seed(store)
    c = local_client(store)
    c.post("/settings/skill-names/merge", data={"a": "react", "b": "react.js", "kept": "react"})
    html = c.get("/changes").text
    assert "Treated react.js as react" in html and "/changes/skill-names/" in html
    lid = store.skill_alias_log()[0]["id"]
    r = c.post(f"/changes/skill-names/{lid}/undo", follow_redirects=True)
    assert store.skill_alias_map() == {} and "no longer count as react" in r.text
    again = c.post(f"/changes/skill-names/{lid}/undo", follow_redirects=True)
    assert "already undone" in again.text
    c.post("/settings/skill-names/split", data={"name": "react"})  # nothing there: not logged
    c.post("/settings/skill-names/merge", data={"a": "python", "b": "python3", "kept": "python"})
    c.post("/settings/skill-names/split", data={"name": "python"})
    log = store.skill_alias_log()
    assert [e["kind"] for e in log][:2] == ["split", "merge"]
    c.post(f"/changes/skill-names/{log[0]['id']}/undo")  # undo the split: merged again
    assert store.skill_alias_map() == {"python3": "python"}
    c.post("/settings/skill-names/reject", data={"a": "node", "b": "nodejs"})
    e = store.skill_alias_log()[0]
    assert e["kind"] == "reject"
    c.post(f"/changes/skill-names/{e['id']}/undo")
    assert not store.hidden_skill_pairs()


# ------------------------------------------------------------------ 4. matched picks column
def test_matched_picks_shows_the_pick_never_the_alias(store):
    store.add_candidate(name="A", tags="react.js")
    store.add_skill_alias("react.js", "react")
    c = store.search_candidates("A").hits[0].candidate
    data = xlsx_export.table_workbook(store, [c], 1, hl=[("react", "red")])
    assert read_xlsx(data)["Candidates"]["rows"][1][1] == "react (1 of 1)"


def test_compare_hints_the_saved_spelling_of_a_merged_skill(store):
    a = store.add_candidate(name="Ann One", tags="data engineering")["id"]
    b = store.add_candidate(name="Bo Two", tags="data engineering, sql")["id"]
    store.add_skill_alias("data engineering", "data engineer")
    html = local_client(store).get(f"/candidates/compare?ids={a},{b}").text
    assert 'title="Saved as data engineering"' in html


# ------------------------------------------------------------------ 5. duplicates
def test_duplicate_groups_start_unticked_except_exact_matches(store):
    for t in ("Data Analyst", "Python Developer"):  # same name and company only: not ticked
        store.add_candidate(name="Kim Haddad", current_company="Acme", current_title=t, location="X")
    store.add_candidate(name="Lee Park", email="lee.park@gmail.com", current_title="Dev")
    store.add_candidate(name="Lee Park", email="leepark+jobs@gmail.com", current_title="Dev")  # same email
    html = local_client(store).get("/candidates/duplicates").text
    boxes = re.findall(r'<input type="checkbox" name="cluster" value="(\d+)"( checked)? aria-label="Merge this group: ([^"]+)"', html)
    state = {name: bool(chk) for _, chk, name in boxes}
    assert state == {"Kim Haddad, 2 profiles": False, "Lee Park, 2 profiles": True}
    assert "Merge 1 group, 1 profile</button>" in html
    assert "<h1>Possible duplicates</h1>" in html and 'id="dup-count"' in html
    assert "Keep all as separate people" in html


def test_kept_person_says_kept_and_different_people_are_not_ticked(store):
    a = store.add_candidate(name="Anais Smith", email="a.smith@gmail.com", current_title="Data Engineer", phone="1")["id"]
    b = store.add_candidate(name="Ana Smith", email="asmith+1@gmail.com", current_title="Data Engineer")["id"]
    c = store.add_candidate(name="Anais Smith", email="asmith+2@gmail.com", current_title="Chef")["id"]
    d = store.add_candidate(name="Anais Smith", email="asmith+3@gmail.com", current_title="Data Engineer")["id"]
    html = local_client(store).get("/candidates/duplicates").text
    assert html.count("data-kept-tag") >= 1 and "<strong>Kept</strong>" in html
    kept = re.search(r'<span class="kept-tag fine" data-kept-tag><strong>Kept</strong></span>\s*<label class="check merge-in" hidden><input type="checkbox"[^>]*>', html)
    assert kept and "disabled" in kept.group(0) and " checked" not in kept.group(0)
    # the chef (different title) and Ana (different first name) start unticked; the kept one is not a merge-in
    ticked_ids = set(re.findall(r'name="m_\d+" value="(\d+)" data-def="1"', html))
    assert str(c) not in ticked_ids and str(b) not in ticked_ids
    group = re.search(r'name="cluster" value="\d+"( checked)?', html)
    assert group and not group.group(1)  # mixed titles: the group is not ticked even with a shared email


def test_duplicates_count_is_groups(store):
    for _ in range(3):
        store.add_candidate(name="Kim Haddad", current_company="Acme")
    assert len(duplicates.find_pairs(store)) == 2 and duplicates.count(store) == 1  # three people, one group


# ------------------------------------------------------------------ 6. CSV import
def test_invalid_email_is_blanked_and_listed_and_a_nameless_row_skipped(store):
    text = "Name,Email,Title\nBad Email,notanemail,Dev\n,,Dev\n,notanemail,Dev\nOk Person,ok@x.com,Dev\n"
    header, rows = importers.read_csv(text)
    mapping, _ = importers.mapping_for(store, header, rows)
    r = importers.import_rows(store, header, rows, mapping, file_name="t.csv", source="csv", backup=False)
    assert r["added"] == 2 and r["skipped_rows"] == 2
    assert any("Row 2" in w and "isn't an email address" in w for w in r["warnings"])
    assert any("Row 3" in s and "no name" in s for s in r["skipped"])
    bad = next(x for x in store.find_by_name("Bad Email"))
    assert not bad.get("email")
    csv_text = importers.skipped_blank_csv(r["skipped"], r["warnings"])
    assert "Saved, a cell left blank" in csv_text and "notanemail" in csv_text


def test_blank_cell_csv_has_one_line_per_row(store):
    lines = ["Row 2: left Email blank, 'x' isn't an email address", "Row 2: left Phone blank, '-5' too short",
             "Row 7: left Email blank, 'y' isn't an email address"]
    out = importers.skipped_blank_csv([], lines).strip().splitlines()
    assert len(out) == 3 and importers.blank_row_count(lines) == 2
    assert "x' isn't an email address; left Phone" in out[1].replace('"', "") or "Phone blank" in out[1]


# ------------------------------------------------------------------ 7. focus and announcements
def test_share_page_announces_leave_outs_and_hides_note_ticks_without_notes(store):
    sofia, maya, _ = three(store)
    c = local_client(store)
    html = c.get(f"/candidates/share?ids={sofia},{maya}").text
    assert 'id="share-live"' in html
    assert re.search(r'name="leave_note"[^>]*disabled', html) and 'data-notes hidden' in html
    html2 = c.get(f"/candidates/share?ids={sofia},{maya}&notes=latest").text
    assert not re.search(r'name="leave_note"[^>]*disabled', html2)
    js = c.get("/static/share.js").text
    assert "say(" in js and "is left out" in js and 't.name === "leave_note"' in js


def test_show_links_and_clear_land_on_the_result_count(store):
    for i in range(120):
        store.add_candidate(name=f"Person {i:03d}", tags="python")
    c = local_client(store)
    html = c.get("/candidates").text
    assert 'id="results-count" tabindex="-1"' in html
    assert 'size=all#results-count' in html and 'size=250#results-count' in html
    assert "Highlight a skill or title</label>" in html
    js = c.get("/static/app.js").text
    assert 'hash === "results-count"' in js and 'hash === "hlcleared"' in js


# ------------------------------------------------------------------ 8. layout
def test_page_size_caption_and_board_css(store):
    for i in range(120):
        store.add_candidate(name=f"Person {i:03d}")
    c = local_client(store)
    html = c.get("/candidates?size=all").text
    assert '<span class="seg">' in html and 'class="fine pagesize-label"' in html and "Showing all 120." in html
    css = c.get("/static/style.css").text
    assert ".board { position: relative; max-width: 100%; }" in css and "#bulk-move { position: sticky;" in css
    assert "newest first (nobody has a label color yet)" in c.get("/candidates").text


def test_job_table_title_is_a_pick_chip(store):
    cid = store.add_candidate(name="Ann Dev", current_title="Data Engineer", tags="python")["id"]
    job = store.add_job("Data Lead", "Acme")
    store.add_to_pipeline(cid, job["id"], "Sourced")
    html = local_client(store).get(f"/jobs/{job['id']}?view=table").text
    assert 'class="chip title-chip"' in html and 'aria-label="Highlight title: Data Engineer"' in html
    assert f"/jobs/{job['id']}?" in html and "title%3Adata+engineer" in html.replace("%3a", "%3A") or "title:data engineer" in html


# ------------------------------------------------------------------ 9. Export for me
def test_share_for_me_says_it_is_for_you_and_defaults_to_latest_note(store):
    sofia, maya, _ = three(store)
    c = local_client(store)
    html = c.get(f"/candidates/share?ids={sofia},{maya}&contact=1&notes=1").text
    assert "<h1>A spreadsheet for you</h1>" in html and "not one to forward" in html
    assert re.search(r'name="notes" value="latest" checked', html)
    html = c.get(f"/candidates/share?ids={sofia},{maya}&contact=1&notes=all").text
    assert re.search(r'name="notes" value="all" checked', html)
    plain = c.get(f"/candidates/share?ids={sofia},{maya}").text
    assert "<h1>Share candidates as a spreadsheet</h1>" in plain and "not one to forward" not in plain


# ------------------------------------------------------------------ B3 bulk at volume
def test_bulk_export_takes_thousands_of_ids(store):
    ids = [store.add_candidate(name=f"Person {i:04d}")["id"] for i in range(1200)]
    c = local_client(store)
    r = c.post("/candidates/bulk", data={"action": "export_xlsx", "candidate_ids": [str(i) for i in ids]})
    assert r.status_code == 200 and len(read_xlsx(r.content)["Candidates"]["rows"]) == 1201
    r = c.post("/candidates/bulk", data={"action": "export_xlsx", "select_all": "1"})
    assert r.status_code == 200 and len(read_xlsx(r.content)["Candidates"]["rows"]) == 1201
    js = c.get("/static/app.js").text
    assert 'sa.name = "select_all"' in js
