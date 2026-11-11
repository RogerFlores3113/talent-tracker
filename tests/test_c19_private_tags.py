"""Private tags: the recruiter's own labels ("Recruiter: Priya", "sourced-nov") kept apart from skills, and never
sent anywhere. Moving them out of Skills in Settings, the remembered list that keeps later writes private, the local
filter, Undo, the v13 -> v14 migration and backup round trip, and the many places they must NOT appear (shares,
exports, write-ups, reports, and every AI app result)."""
from __future__ import annotations

import io
import json
import re
import sqlite3
import zipfile

import anyio
import pytest
from conftest import local_client

from talent import chase, importers, reports, typesense_index, xlsx_export
from talent.mcp_guard import strip_private
from talent.mcp_server import build_server
from talent.private_tags import looks_private, settings_view
from talent.store import Store

STAGES = ["Sourced", "Screening", "Submitted to client", "Interview", "Hired", "Rejected", "Withdrawn"]
LABELS = ("Recruiter: Priya", "sourced-nov")


def _tool(store, name):
    return build_server(store)._tool_manager._tools[name].fn


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def dump(result) -> str:
    """Everything an AI app would read from a tool result: its text and its structured data."""
    if isinstance(result, tuple):
        content, data = result
    else:
        content, data = getattr(result, "content", []), getattr(result, "structuredContent", None)
    text = "\n".join(getattr(c, "text", "") or "" for c in content)
    return text + "\n" + json.dumps(data, default=str)


def world(store):
    job = store.add_job("Platform Engineer", "Acme", stages=STAGES)
    ana = store.add_candidate(name="Ana Ruiz", email="ana@example.com", about="Builds APIs.",
                              current_title="Staff Engineer", current_company="Initech",
                              tags="Python, Go, Recruiter: Priya, sourced-nov")
    store.add_to_pipeline(ana["id"], job["id"], "Screening")
    return job, ana


def make_private(client, *tags):
    for t in tags:
        r = client.post("/settings/private-tags/make", data={"tag": t}, follow_redirects=False)
        assert r.status_code == 303


def blob_of(xlsx: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(xlsx))
    return "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())


def assert_clean(text: str, where: str) -> None:
    low = text.lower()
    for label in LABELS:
        assert label.lower() not in low, f"{label!r} leaked into {where}"


@pytest.fixture
def ready(store):
    job, ana = world(store)
    c = local_client(store)
    make_private(c, *LABELS)
    return store, job, store.get_candidate(ana["id"]), c


# ------------------------------------------------------------------ the Settings action
def test_make_private_moves_the_tag_for_everyone_and_says_how_many(store):
    job, ana = world(store)
    ben = store.add_candidate(name="Ben Cho", tags="Rust, sourced-nov")
    c = local_client(store)
    r = c.post("/settings/private-tags/make", data={"tag": "Sourced-Nov"}, follow_redirects=True)
    assert r.status_code == 200
    msg = re.search(r'id="private-result"[^>]*>.*?<p class="flash private-result">(.*?)</p>', r.text, re.S).group(1)
    assert "Made Sourced-Nov private" in msg and "2 people changed" in msg
    assert 'role="status"' in r.text and "Undo" in r.text
    assert store.get_candidate(ana["id"])["tags"] == ["Python", "Go", "Recruiter: Priya"]
    assert store.get_candidate(ana["id"])["private_tags"] == ["sourced-nov"]
    assert store.get_candidate(ben["id"])["tags"] == ["Rust"]
    assert store.private_tag_names() == ["Sourced-Nov"]
    assert "sourced-nov" not in [t["tag"].lower() for t in store.list_tags(50)]


def test_undo_moves_it_back_and_drops_it_from_the_list(ready):
    store, job, ana, c = ready
    r = c.post("/settings/private-tags/undo", data={"tag": "sourced-nov"}, follow_redirects=True)
    assert "is a skill again" in r.text
    got = store.get_candidate(ana["id"])
    assert "sourced-nov" in got["tags"] and "sourced-nov" not in got["private_tags"]
    assert [n.lower() for n in store.private_tag_names()] == ["recruiter: priya"]
    store.update_candidate(ana["id"], tags="Python, sourced-nov")  # no longer on the list: stays a skill
    assert "sourced-nov" in store.get_candidate(ana["id"])["tags"]


def test_settings_lists_label_looking_tags_first_and_pages_the_rest(store):
    for i in range(230):
        store.add_candidate(name=f"P{i}", tags=f"skill{i:03d}")
    store.add_candidate(name="Q", tags="Recruiter: Priya, dnc, referral from Sam, hot, nov-2025, Python, owner@x")
    v = settings_view(store)
    assert {t["tag"] for t in v["suggested"]} >= {"Recruiter: Priya", "dnc", "referral from Sam", "hot", "nov-2025", "owner@x"}
    assert all(not looks_private(t["tag"]) for t in v["others"]) and len(v["others"]) == 200 and v["pages"] == 2
    assert settings_view(store, "skill229")["others_total"] == 1
    assert not looks_private("open to work") and looks_private("to call") and looks_private("sourced-nov")
    page = local_client(store).get("/settings").text
    assert 'id="private-tags"' in page and "stay on this computer" in page
    assert 'aria-label="Make dnc private for 1 person"' in page and "Next page" in page
    assert "Page 1 of 2" in page


def test_one_person_make_private_leaves_everyone_else(store):
    job, ana = world(store)
    ben = store.add_candidate(name="Ben Cho", tags="Rust, sourced-nov")
    c = local_client(store)
    form = c.get(f"/candidates/{ana['id']}?edit=1").text
    assert 'aria-label="Make sourced-nov private for this person"' in form and 'name="private_tags"' in form
    r = c.post(f"/candidates/{ana['id']}/private-tags", data={"tag": "sourced-nov"}, follow_redirects=False)
    assert r.status_code == 303
    assert store.get_candidate(ana["id"])["private_tags"] == ["sourced-nov"]
    assert "sourced-nov" in store.get_candidate(ben["id"])["tags"] and store.private_tag_names() == []


def test_candidate_page_shows_private_tags_apart_and_the_filter_finds_them(ready):
    store, job, ana, c = ready
    page = c.get(f"/candidates/{ana['id']}").text
    assert "Private tags" in page and "only on this computer" in page and "sourced-nov" in page
    assert "Recruiter: Priya" in page
    skills_area = page.split('id="private-tags"')[0]
    assert "sourced-nov" not in skills_area
    form = c.get(f"/candidates/{ana['id']}?edit=1").text
    assert 'name="private_tags"' in form and "Recruiter: Priya, sourced-nov" in form
    store.add_candidate(name="Zed Other", tags="Java")
    table = c.get("/candidates?tag=sourced-nov").text
    assert "Ana Ruiz" in table and "Zed Other" not in table
    # the same filter from the AI app's side (no local view) finds nothing
    hits = store.search_candidates("", tag="sourced-nov").hits
    assert hits == []


def test_edit_form_saves_private_tags_and_blank_clears_them(ready):
    store, job, ana, c = ready
    c.post(f"/candidates/{ana['id']}", data={"name": "Ana Ruiz", "tags": "Python", "private_tags": "dnc"})
    got = store.get_candidate(ana["id"])
    assert got["private_tags"] == ["dnc"] and got["tags"] == ["Python"]
    c.post(f"/candidates/{ana['id']}", data={"name": "Ana Ruiz", "tags": "Python", "private_tags": ""})
    assert store.get_candidate(ana["id"])["private_tags"] == []


# ------------------------------------------------------------------ later writes stay private
def test_csv_import_and_save_again_land_in_private_tags(ready):
    store, job, ana, c = ready
    header, rows = importers.read_csv("Name,Email,Skills\nNew Person,new@x.com,\"Go, sourced-nov\"\n"
                                      "Ana Ruiz,ana@example.com,\"Recruiter: Priya, Kafka\"\n")
    importers.import_rows(store, header, rows, ["name", "email", "tags"], backup=False)
    new = store.get_candidate(store.find_duplicate("new@x.com")["id"])
    assert new["tags"] == ["Go"] and new["private_tags"] == ["sourced-nov"]
    again = store.get_candidate(ana["id"])
    assert "Kafka" in again["tags"] and "Recruiter: Priya" not in again["tags"]
    assert set(again["private_tags"]) == {"Recruiter: Priya", "sourced-nov"}


def test_csv_column_can_map_to_private_tags_and_the_full_export_keeps_them(ready):
    store, job, ana, c = ready
    header, rows = importers.read_csv("Name,Email,Private tags\nPat Lee,pat@x.com,\"dnc, Owner: Sam\"\n")
    importers.import_rows(store, header, rows, ["name", "email", "private_tags"], backup=False)
    pat = store.get_candidate(store.find_duplicate("pat@x.com")["id"])
    assert pat["private_tags"] == ["dnc", "Owner: Sam"] and pat["tags"] == []
    full = importers.export_csv(store, include_private=True)
    assert "Private tags" in full and "Recruiter: Priya" in full and "dnc" in full
    assert c.get("/export.csv").content.decode("utf-8-sig").count("Recruiter: Priya") == 1  # the recruiter's own backup
    assert_clean(importers.export_csv(store), "the table CSV")
    assert_clean(xlsx_export.csv_in_order(store, [ana["id"]]).decode("utf-8-sig"), "csv_in_order")


def test_mcp_update_and_save_put_a_listed_skill_in_private_tags_and_do_not_echo_it(ready):
    store, job, ana, c = ready
    store.update_candidate(ana["id"], tags="Python")
    out = _tool(store, "update_candidate")(candidate_id=ana["id"], tags=["Python", "Rust", "dnc"])
    assert_clean(dump(out), "update_candidate")
    got = store.get_candidate(ana["id"])
    assert got["tags"] == ["Python", "Rust", "dnc"]  # dnc is not on the list yet
    store.make_tag_private("dnc")
    out = _tool(store, "update_candidate")(candidate_id=ana["id"], tags=["Python", "sourced-nov", "dnc"])
    text = dump(out)
    assert_clean(text, "update_candidate") and "dnc" not in text.lower().replace("update", "")
    got = store.get_candidate(ana["id"])
    assert got["tags"] == ["Python"] and set(got["private_tags"]) >= {"sourced-nov", "dnc", "Recruiter: Priya"}
    out = _tool(store, "save_candidate")(name="Ana Ruiz", email="ana@example.com", tags=["Python", "sourced-nov"])
    assert_clean(dump(out), "save_candidate") and "dnc" not in dump(out).lower()
    assert "sourced-nov" in store.get_candidate(ana["id"])["private_tags"]
    new = _tool(store, "save_candidate")(name="Cy New", email="cy@x.com", tags=["Go", "Recruiter: Priya"])
    assert "Recruiter" not in dump(new)
    assert store.get_candidate(store.find_duplicate("cy@x.com")["id"])["private_tags"] == ["Recruiter: Priya"]


def test_the_ai_app_cannot_send_or_overwrite_private_tags(ready):
    store, job, ana, c = ready
    server = build_server(store)
    try:
        call(server, "update_candidate", candidate_id=ana["id"], private_tags=["x"])
    except Exception:
        pass
    assert set(store.get_candidate(ana["id"])["private_tags"]) == set(LABELS)
    call(server, "update_candidate", candidate_id=ana["id"], tags=["Python"])  # replaces skills only
    assert set(store.get_candidate(ana["id"])["private_tags"]) == set(LABELS)


def test_changes_to_private_tags_are_not_on_the_timeline(ready):
    store, job, ana, c = ready
    store.update_candidate(ana["id"], private_tags="dnc, sourced-nov")
    store.update_candidate(ana["id"], tags="Python, Go", about="Builds APIs well.")
    rows = json.dumps(store.timeline(ana["id"]), default=str)
    assert "dnc" not in rows and clean_of(rows)
    out = dump(_tool(store, "get_candidate")(candidate_id=ana["id"]))
    assert "dnc" not in out


def clean_of(text: str) -> bool:
    return all(label.lower() not in text.lower() for label in LABELS)


def test_earlier_change_entries_are_scrubbed_when_a_tag_goes_private(store):
    job, ana = world(store)
    store.update_candidate(ana["id"], tags="Python, Go, sourced-nov, Kafka")  # an old save that names it
    store.update_candidate(ana["id"], tags="Python, sourced-nov")
    assert "sourced-nov" in json.dumps(store.timeline(ana["id"]), default=str)
    store.make_tag_private("sourced-nov")
    assert "sourced-nov" not in json.dumps(store.timeline(ana["id"]), default=str)


def test_merge_keeps_both_peoples_private_tags(store):
    a = store.add_candidate(name="Ana Diaz", email="a@x.com", private_tags="dnc")
    b = store.add_candidate(name="Ana Diaz", email="b@x.com", private_tags="hot", tags="Go")
    merged = store.merge_candidates(a["id"], b["id"])
    assert set(merged["private_tags"]) == {"dnc", "hot"} and merged["tags"] == ["Go"]
    rows = json.dumps(store.timeline(a["id"]), default=str)
    assert "dnc" not in rows and '"hot"' not in rows
    again, created = store.upsert_candidate(name="Ana Diaz", email="a@x.com", private_tags="sourced-nov")
    assert not created and set(again["private_tags"]) == {"dnc", "hot", "sourced-nov"}
    assert store._row("SELECT count(*) AS n FROM candidate_private_tags WHERE candidate_id = ?", (a["id"],))["n"] == 3


# ------------------------------------------------------------------ never leaves the computer
def test_not_in_the_web_share_file_or_the_other_workbooks(ready):
    store, job, ana, c = ready
    res = c.post("/candidates/share", data={"ids": str(ana["id"])})
    assert res.status_code == 200
    blob = blob_of(res.content)
    assert "Python, Go" in blob  # the skills are there, the labels are not
    assert_clean(blob, "the share file")
    with_job = c.post("/candidates/share", data={"ids": str(ana["id"]), "job_id": str(job["id"])})
    assert_clean(blob_of(with_job.content), "the share file for a job")
    assert_clean(blob_of(c.get("/export.xlsx").content), "the table workbook")
    data, _, _ = xlsx_export.share_workbook(store, [ana["id"]], job_id=job["id"])
    assert_clean(blob_of(data), "share_workbook")
    assert_clean(json.dumps(xlsx_export.share_table(store, [ana["id"]], job_id=job["id"]), default=str), "share_table")


def test_not_in_the_chat_export(ready, tmp_path, monkeypatch):
    store, job, ana, c = ready
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(tmp_path / "dl"))
    out = _tool(store, "export_candidates_xlsx")(candidate_ids=[ana["id"]], job_id=job["id"])
    text = dump(out)
    assert_clean(text, "the export reply")
    files = list((tmp_path / "dl").iterdir())
    assert files
    for f in files:
        blob = "\n".join(zipfile.ZipFile(f).read(n).decode("utf-8", "replace") for n in zipfile.ZipFile(f).namelist())
        assert "Python" in blob
        assert_clean(blob, "the chat export file")


def test_not_in_the_submit_write_up_web_or_mcp(ready):
    store, job, ana, c = ready
    page = c.get(f"/jobs/{job['id']}/submit/{ana['id']}").text
    assert "Skills: Python, Go" in page and "never your private tags" in page
    assert_clean(page, "the submit page")
    w = chase.writeup(store, ana["id"], job["id"])
    assert_clean(json.dumps(w, default=str), "chase.writeup")
    text = dump(_tool(store, "submit_to_client")(candidate_id=ana["id"], job_id=job["id"]))
    assert "Python, Go" in text and "never your private tags" in text
    assert_clean(text, "submit_to_client")


def test_not_in_client_reports(ready):
    store, job, ana, c = ready
    rep = reports.report(store, "acme")
    assert_clean(reports.report_text(rep) + json.dumps(rep, default=str), "the client report")
    assert_clean(dump(_tool(store, "client_report")(client="acme")), "client_report")
    assert_clean(json.dumps(reports.overview(store), default=str), "the clients overview")
    assert_clean(c.get("/clients/acme").text, "the client page")


def test_not_in_any_ai_app_result_or_in_chat_view(ready):
    store, job, ana, c = ready
    for run in (lambda: _tool(store, "get_candidate")(candidate_id=ana["id"]),
                lambda: _tool(store, "search_candidates")(query="Ana"),
                lambda: _tool(store, "candidate_cards")(candidate_ids=[ana["id"]]),
                lambda: _tool(store, "compare_candidates")(candidate_ids=[ana["id"]]),
                lambda: call(build_server(store), "get_candidate", candidate_id=ana["id"]),
                lambda: call(build_server(store), "search_candidates", query="Ana"),
                lambda: call(build_server(store), "candidate_cards", candidate_ids=[ana["id"]])):
        try:
            res = run()
        except Exception as exc:  # a tool that needs more than this world has: its error must be clean too
            res = str(exc)
        assert_clean(dump(res) if not isinstance(res, str) else res, "an AI app result")
    got = dump(_tool(store, "get_candidate")(candidate_id=ana["id"]))
    assert "Python" in got and "private_tags" not in got
    assert "private_tags" not in dump(call(build_server(store), "get_candidate", candidate_id=ana["id"]))


def test_search_does_not_let_the_ai_app_probe_private_tags(ready):
    store, job, ana, c = ready
    for args in ({"skills": ["sourced-nov"]}, {"query": "sourced-nov"}, {"query": "skill:Recruiter"}):
        res = _tool(store, "search_candidates")(**args)
        assert "Ana Ruiz" not in dump(res), args


def test_strip_private_removes_the_key_anywhere():
    got = strip_private({"a": [{"private_tags": ["x"], "b": 1}], "old": {"private_tags": "y", "tags": "z"}})
    assert got == {"a": [{"b": 1}], "old": {"tags": "z"}}


def test_not_in_typesense_documents_or_compare_and_picks(ready):
    store, job, ana, c = ready
    doc = typesense_index.to_document(store.get_candidate(ana["id"]))
    assert_clean(json.dumps(doc), "the Typesense document")


def test_review_text_for_the_ai_app_does_not_name_private_tags(ready):
    from talent import review
    store, job, ana, c = ready
    queued = review.queue_profile(store, ana["id"], {"tags": ["Python", "sourced-nov"]}, None, ["tags"], None)
    assert_clean(json.dumps(queued["summary"]) + "\n".join(review.ai_lines(store, candidate_id=ana["id"])), "review lines")


# ------------------------------------------------------------------ storage
def test_migration_from_v13_keeps_data_and_adds_private_tags(tmp_path):
    path = tmp_path / "v13.db"
    s = Store(path)
    ana = s.add_candidate(name="Ana", tags="Python, sourced-nov")
    s.close()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE candidate_private_tags")
    con.execute("ALTER TABLE candidates DROP COLUMN private_tags")
    from talent.store import _MIGRATIONS  # v14 is the last step, wherever the version comments sit
    at = next(i for i, m in enumerate(_MIGRATIONS, start=1) if isinstance(m, str) and "ADD COLUMN private_tags" in m)
    assert at == len(_MIGRATIONS)
    con.execute(f"PRAGMA user_version = {at - 1}")
    con.commit()
    con.close()
    s = Store(path)
    assert s.schema_version == len(_MIGRATIONS)
    got = s.get_candidate(ana["id"])
    assert got["tags"] == ["Python", "sourced-nov"] and got["private_tags"] == []
    s.make_tag_private("sourced-nov")
    assert s.get_candidate(ana["id"])["private_tags"] == ["sourced-nov"]
    assert s._row("SELECT count(*) AS n FROM candidate_private_tags")["n"] == 1
    s.close()


def test_private_tags_are_not_in_the_search_index(ready):
    store, job, ana, c = ready
    assert store.search_candidates("sourced-nov").hits == []
    assert store.search_candidates("Priya").hits == []
    assert [h.candidate["id"] for h in store.search_candidates("Python").hits] == [ana["id"]]
    cols = [r[1] for r in store._conn.execute("PRAGMA table_info(candidates_fts)")]
    assert "private_tags" not in cols


def test_backup_and_restore_round_trip_private_tags(tmp_path):
    s = Store(tmp_path / "live.db")
    a = s.add_candidate(name="Ana", tags="Python, sourced-nov, dnc")
    s.make_tag_private("sourced-nov")
    snap = tmp_path / "snap.db"
    s.backup_to(snap)
    s.make_tag_skill("sourced-nov")
    s.update_candidate(a["id"], private_tags="")
    assert s.get_candidate(a["id"])["private_tags"] == []
    s.restore_from(snap)
    got = s.get_candidate(a["id"])
    assert got["private_tags"] == ["sourced-nov"] and got["tags"] == ["Python", "dnc"]
    assert s.private_tag_names() == ["sourced-nov"]
    assert s._row("SELECT count(*) AS n FROM candidate_private_tags")["n"] == 1
    s.close()


def test_the_wording_says_private_tags_are_never_included(ready):
    store, job, ana, c = ready
    assert "never your private tags" in c.get(f"/candidates/share?ids={ana['id']}").text
    assert "whole tag list" not in c.get(f"/candidates/share?ids={ana['id']}").text
