"""Fixes after the private tags panel: Restore keeps them private, the full CSV says what it holds, filtered exports
never print a private tag name, the AI write reply gives no oracle, and the onboarding links and messages."""
from __future__ import annotations

import io
import re
import zipfile

import pytest
from conftest import flash_of, local_client
from test_c19_private_tags import assert_clean, blob_of, call, dump, make_private, ready, world  # noqa: F401

from talent import importers, paths
from talent.mcp_server import build_server
from talent.store import AI_PRIVATE_LINE, Store


@pytest.fixture
def disk_store():
    s = Store(paths.db_path())
    yield s
    s.close()


# ------------------------------------------------------------------ 1. restore
def test_restore_keeps_private_tags_private(disk_store):
    s = disk_store
    ana = s.add_candidate(name="Ana Ruiz", email="ana@example.com", tags="Python, Recruiter: Priya, sourced-nov")
    old = importers.backup_before_import(s, reason="delete")  # made before anything was private
    c = local_client(s)
    make_private(c, "Recruiter: Priya", "sourced-nov")
    r = c.post("/settings/backups/restore", data={"name": old.name}, follow_redirects=False)
    assert r.status_code == 303
    assert "2 labels kept in Private tags" in flash_of(r)
    got = s.get_candidate(ana["id"])
    assert got["tags"] == ["Python"]
    assert sorted(got["private_tags"]) == ["Recruiter: Priya", "sourced-nov"]
    assert {n.casefold() for n in s.private_tag_names()} == {"recruiter: priya", "sourced-nov"}
    share = c.post("/candidates/share", data={"ids": str(ana["id"])})
    assert_clean(blob_of(share.content), "the share file after a restore")


def test_restore_says_nothing_when_no_labels_moved(disk_store):
    s = disk_store
    s.add_candidate(name="Bo Lee", tags="Python")
    old = importers.backup_before_import(s, reason="delete")
    r = local_client(s).post("/settings/backups/restore", data={"name": old.name}, follow_redirects=False)
    assert "kept in Private tags" not in flash_of(r)


# ------------------------------------------------------------------ 2. full CSV label
def test_full_csv_is_labelled_for_own_records(ready):
    store, job, ana, c = ready
    page = c.get("/settings").text
    assert re.search(r'href="/export\.csv"[^>]*>[^<]*private tags and notes', page)
    assert "do not send it to anyone" in page
    assert "—" not in page.split('id="full-csv-note"')[1].split("</p>")[0]
    assert "Recruiter: Priya" in c.get("/export.csv").content.decode("utf-8-sig")  # contents unchanged


# ------------------------------------------------------------------ 3. filter lines
def test_filtered_export_never_prints_a_private_tag(ready):
    store, job, ana, c = ready
    res = c.get("/candidates/export.xlsx", params={"tag": "sourced-nov"})
    assert res.status_code == 200
    blob = blob_of(res.content)
    assert "a private tag" in blob
    assert "sourced-nov" not in blob.lower()
    plain = c.get("/candidates/export.xlsx", params={"tag": "Python"})
    assert "Python" in blob_of(plain.content)
    assert "a private tag" not in blob_of(plain.content)


def test_skills_filter_line_hides_a_private_name(ready):
    store, job, ana, c = ready
    blob = blob_of(c.get("/candidates/export.xlsx", params={"skills": "Python,sourced-nov"}).content)
    assert "sourced-nov" not in blob.lower()


# ------------------------------------------------------------------ 4. AI write oracle
def _tool(store, name):
    return build_server(store)._tool_manager._tools[name].fn


def test_ai_write_reply_has_the_same_shape_with_or_without_a_private_name(ready):
    store, job, ana, c = ready
    b = store.add_candidate(name="Bo Lee", email="bo@example.com", tags="Python")
    with_private = dump(call(build_server(store), "update_candidate", candidate_id=ana["id"], tags=["Python", "sourced-nov", "Rust"]))
    without = dump(call(build_server(store), "update_candidate", candidate_id=b["id"], tags=["Python", "Rust"]))
    for text in (with_private, without):
        assert "Skills saved" in text
        assert '"tags"' not in text and "Skills:" not in text
    assert "sourced-nov" not in with_private.lower()
    assert '"changed": ["tags"]' not in with_private and '"changed": ["tags"]' not in without


def test_ai_write_with_only_a_private_name_reads_like_any_skill_write(ready):
    store, job, ana, c = ready
    out = dump(call(build_server(store), "update_candidate", candidate_id=ana["id"], tags=["Python", "Go", "sourced-nov"]))
    assert "Skills saved" in out and "Nothing to change" not in out
    saved = dump(call(build_server(store), "save_candidate", name="Cy Wu", email="cy@example.com", tags=["Go", "sourced-nov"]))
    assert "Skills:" not in saved and '"tags"' not in saved and "sourced-nov" not in saved.lower()
    again = dump(call(build_server(store), "save_candidate", name="Cy Wu", email="cy@example.com", tags=["Go", "Rust", "sourced-nov"]))
    assert "Skills saved" in again and "sourced-nov" not in again.lower()


def test_ai_write_to_a_private_name_leaves_a_timeline_line_with_no_name(ready):
    store, job, ana, c = ready
    call(build_server(store), "update_candidate", candidate_id=ana["id"], tags=["Python", "sourced-nov"])
    lines = [t["body"] for t in store.timeline(ana["id"], limit=50)]
    assert AI_PRIVATE_LINE in lines
    assert not any("sourced-nov" in (b or "") for b in lines)
    got = dump(call(build_server(store), "get_candidate", candidate_id=ana["id"]))
    assert AI_PRIVATE_LINE in got and "sourced-nov" not in got.lower()


def test_a_web_save_leaves_no_ai_timeline_line(ready):
    store, job, ana, c = ready
    store.update_candidate(ana["id"], tags="Python, sourced-nov")
    assert AI_PRIVATE_LINE not in [t["body"] for t in store.timeline(ana["id"], limit=50)]


# ------------------------------------------------------------------ 5. onboarding
def test_settings_jump_bar_has_private_tags(ready):
    store, job, ana, c = ready
    page = c.get("/settings").text
    nav = page.split('aria-label="Jump to"')[1].split("</nav>")[0]
    assert 'href="#private-tags"' in nav and "Private tags" in nav


def test_share_page_and_edit_form_link_to_private_tags(ready):
    store, job, ana, c = ready
    share = c.get("/candidates/share", params={"ids": str(ana["id"])}).text
    assert re.search(r'href="/settings#private-tags"', share)
    form = c.get(f"/candidates/{ana['id']}?edit=1").text
    assert re.search(r'href="/settings#private-tags"', form)


def test_edit_save_that_diverts_labels_says_how_many(ready):
    store, job, ana, c = ready
    eve = store.add_candidate(name="Eve Chen", email="eve@example.com", tags="Python")
    r = c.post(f"/candidates/{eve['id']}", data={"name": "Eve Chen", "tags": "Python, Go, Recruiter: Priya, sourced-nov"},
               follow_redirects=False)
    assert r.status_code == 303
    assert "2 labels went to Private tags" in flash_of(r)
    assert store.get_candidate(eve["id"])["tags"] == ["Python", "Go"]
    plain = c.post(f"/candidates/{eve['id']}", data={"name": "Eve Chen", "tags": "Python, Go"}, follow_redirects=False)
    assert "went to Private tags" not in flash_of(plain)


def test_web_import_says_how_many_labels_went_private(ready):
    store, job, ana, c = ready
    csv_bytes = b"Name,Email,Skills\nDee Park,dee@example.com,\"Go, sourced-nov, Recruiter: Priya\"\n"
    r = c.post("/import", files={"file": ("p.csv", csv_bytes)})
    token = re.search(r'name="token" value="(\w+)"', r.text).group(1)
    done = c.post("/import/run", data={"token": token, "col0": "name", "col1": "email", "col2": "tags"})
    assert "2 labels went to Private tags" in done.text
    dee = store.find_duplicate("dee@example.com")
    assert store.get_candidate(dee["id"])["tags"] == ["Go"]


def test_empty_search_hints_at_the_tag_filter(ready):
    store, job, ana, c = ready
    page = c.get("/candidates", params={"q": "sourced-nov"}).text
    assert "Private tags are found with the tag filter" in page
    assert 'href="/candidates?tag=sourced-nov"' in page
    assert "Private tags are found with the tag filter" not in c.get("/candidates", params={"q": "zzzqq"}).text
    assert "Private tags are found with the tag filter" not in c.get("/candidates", params={"q": "Python"}).text


def test_make_private_on_the_edit_form_keeps_unsaved_edits(ready):
    store, job, ana, c = ready
    store.update_candidate(ana["id"], tags="Python, Go, Chase Friday")
    form = c.get(f"/candidates/{ana['id']}?edit=1").text
    assert 'name="make_private" value="Chase Friday"' in form
    assert 'aria-label="Make Chase Friday private for this person"' in form
    assert 'form="private-move"' not in form
    r = c.post(f"/candidates/{ana['id']}", data={"name": "Ana Ruiz", "current_title": "Principal Engineer",
                                                 "tags": "Python, Go, Chase Friday", "make_private": "Chase Friday"},
               follow_redirects=False)
    assert r.status_code == 303
    got = store.get_candidate(ana["id"])
    assert got["current_title"] == "Principal Engineer"  # the unsaved edit was kept
    assert "Chase Friday" not in got["tags"] and "Chase Friday" in got["private_tags"]
    assert "private tag" in flash_of(r)


def test_private_highlight_pick_is_never_printed_in_a_file(tmp_path):
    """A highlight pick named like a private tag is dropped from files that may be sent on."""
    import io
    import zipfile
    from talent import xlsx_export
    from talent.store import Store
    s = Store(tmp_path / "p.db")
    c = s.add_candidate(name="Pat Lee", tags=["python", "sourced-nov"])
    s.make_tag_private("sourced-nov")
    hl = [("python", "red"), ("sourced-nov", "yellow")]
    for data in (xlsx_export.share_workbook(s, [c["id"]], hl=hl)[0],
                 xlsx_export.table_workbook(s, [s.get_candidate(c["id"])], 1, hl=hl)):
        z = zipfile.ZipFile(io.BytesIO(data))
        text = "".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n.endswith(".xml"))
        assert "sourced-nov" not in text and "python (red)" in text
    assert "sourced-nov" not in xlsx_export.csv_in_order(s, [c["id"]], hl=hl).decode()
