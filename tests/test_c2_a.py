"""Cycle 2 fixer A: identity and data safety."""
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest
from mcp.types import CallToolResult

from conftest import local_client
from talent import importers
from talent.mcp_server import build_server
from talent.paths import backups_dir
from talent.sample_data import load_sample_data, remove_sample_data, sample_ids
from talent.store import IdentityConflict, Store, fold_name, same_person_name


def call(server, tool, **args):
    return anyio.run(lambda: server.call_tool(tool, args))


def data(result):
    if isinstance(result, CallToolResult):
        return result.structuredContent
    return result[1].get("result", result[1])


def text(result):
    content = result.content if isinstance(result, CallToolResult) else result[0]
    return "\n".join(c.text for c in content)


def notice(response):
    return parse_qs(urlsplit(response.headers["location"]).query).get("notice", [""])[0]


def backups():
    return sorted(f.name for f in backups_dir().iterdir())


# ------------------------------------------------------------ 1. identity
def test_a_shared_first_name_is_not_enough():
    for a, b in (("Maya Smith", "Maya Okafor"), ("Daniel Rivera", "Daniel Reyes"), ("Lee Chen", "Someone Else"),
                 ("Maya Ann Smith", "Maya Ann Okafor")):
        assert not same_person_name(a, b), (a, b)
    for a, b in (("Priya R.", "Priya Raman"), ("Okafor Maya", "Maya Okafor"), ("Raman, P.", "Priya Raman"),
                 ("Maya J. Okafor", "Maya Okafor"), ("Maya", "Maya Okafor"), ("Dan Reyes", "Daniel Reyes"),
                 ("Jose Alvarez", "José Álvarez")):
        assert same_person_name(a, b), (a, b)
    assert fold_name("José  Álvarez-Ruiz") == "jose alvarez ruiz"


def test_a_sheet_row_with_another_last_name_doesnt_overwrite(store):
    maya = store.add_candidate(name="Maya Okafor", email="maya@x.com", current_company="Ledgerline")
    with pytest.raises(IdentityConflict):
        store.upsert_candidate(name="Maya Smith", email="maya@x.com", current_company="OtherCo")
    assert store.get_candidate(maya["id"])["current_company"] == "Ledgerline"


# ---------------------------------------------- 2. update_candidate guards
def test_ai_changes_one_identity_field_per_call(store):
    lee = store.add_candidate(name="Lee Chen", email="lee@x.com", phone="555 0100")
    server = build_server(store)
    with pytest.raises(Exception, match="fields that say who the person is"):
        call(server, "update_candidate", candidate_id=lee["id"], email="harvest@evil.example", phone="555 9999")
    with pytest.raises(Exception, match="doesn't look like the same person"):
        call(server, "update_candidate", candidate_id=lee["id"], name="Someone Else")
    assert store.get_candidate(lee["id"])["email"] == "lee@x.com"
    # Same values passed again don't count as changes, and one real change goes through.
    out = data(call(server, "update_candidate", candidate_id=lee["id"], email="LEE@x.com", phone="555 0101",
                    name="Lee Chen"))
    assert out["changed"] == ["phone"]
    # A second identity change within 10 minutes waits for the recruiter (tests/test_c2_f.py).
    with pytest.raises(Exception, match="changed this person's phone a few minutes ago"):
        call(server, "update_candidate", candidate_id=lee["id"], name="Lee Chen-Park")


# ----------------------------------------------------- 3. save_candidate
def test_save_asks_before_creating_a_same_name_person(store):
    jose = store.add_candidate(name="José Álvarez", current_title="SRE")
    server = build_server(store)
    res = call(server, "save_candidate", name="Jose Alvarez", current_title="Engineer")
    out = data(res)
    assert out["created"] is False and out["needs_confirmation"] and out["saved_status"] == "Not saved"
    assert out["possible_duplicates"][0]["id"] == jose["id"] and store.count_candidates() == 1
    assert "create_anyway=true" in text(res)
    out = data(call(server, "save_candidate", name="Jose Alvarez", current_title="Engineer", create_anyway=True))
    assert out["created"] is True and store.count_candidates() == 2


def test_save_shows_the_candidate_view_and_keeps_history_in_step(store):
    server = build_server(store)
    tool = {t.name: t for t in anyio.run(server.list_tools)}["save_candidate"]
    assert tool.meta and "ui" in str(tool.meta)
    res = call(server, "save_candidate", name="Maya Okafor", email="maya@x.com", current_title="Backend Engineer",
               current_company="Ledgerline", work_history=[{"title": "Backend Engineer", "company": "Ledgerline",
                                                             "start": "2021"}, {"title": "Dev", "company": "Stripe",
                                                                                "start": "2017", "end": "2021"}])
    out = data(res)
    assert out["saved_status"] == "Saved" and out["name"] == "Maya Okafor" and out["app_url"].endswith(
        f"/candidates/{out['id']}") and out["candidate"]["id"] == out["id"] and text(res).startswith("Saved.")
    # A promotion at the same company updates the current role in place.
    out = data(call(server, "save_candidate", name="Maya Okafor", email="maya@x.com",
                    current_title="Senior Backend Engineer"))
    assert out["saved_status"].startswith("Updated: title") and "work_history" in out["changed"]
    assert [w["title"] for w in store.work_history(out["id"])] == ["Senior Backend Engineer", "Dev"]
    # A new company ends the old role and puts the new one on top.
    out = data(call(server, "save_candidate", name="Maya Okafor", email="maya@x.com", current_title="Staff Engineer",
                    current_company="Ramp"))
    hist = store.work_history(out["id"])
    assert (hist[0]["title"], hist[0]["company"], hist[0]["end"]) == ("Staff Engineer", "Ramp", None)
    assert hist[1]["company"] == "Ledgerline" and hist[1]["end"]
    # Nothing new: no change.
    out = data(call(server, "save_candidate", name="Maya Okafor", email="maya@x.com", current_company="Ramp"))
    assert out["saved_status"] == "Already saved, nothing new" and out["changed"] == []


# --------------------------------------------------------- 4. stable ids
def test_deleted_ids_are_never_reused(tmp_path):
    s = Store(tmp_path / "ids.db")
    a, b = s.add_candidate(name="A"), s.add_candidate(name="B")
    s.delete_candidate(b["id"])
    j = s.add_job("Job one")
    s.delete_job(j["id"])
    s.close()
    s = Store(tmp_path / "ids.db")
    c = s.add_candidate(name="C")
    assert c["id"] == b["id"] + 1 and a["id"] < b["id"]
    assert s.add_job("Job two")["id"] == j["id"] + 1
    s.close()


# ------------------------------------------------------- 5. sample data
def test_remove_sample_data_keeps_what_the_recruiter_touched(store):
    real = store.add_candidate(name="Jordan Pierce")
    load_sample_data(store)
    people, jobs = sample_ids(store)
    by_name = {store.get_candidate(i)["name"]: i for i in people}
    by_title = {store.get_job(j)["title"]: j for j in jobs}
    store.log(by_name["Maya Okafor"], "call", "Real call about the Austin role", source="App")
    store.move_stage(by_name["Kenji Watanabe"], by_title["Senior Backend Engineer"], "Offer")
    store.add_to_pipeline(real["id"], by_title["Data Lead"])
    r = remove_sample_data(store)
    assert r["kept_people"] == ["Kenji Watanabe", "Maya Okafor"] and r["people"] == 14
    assert sorted(r["kept_jobs"]) == ["Data Lead (Acme Payments)", "Senior Backend Engineer (Acme Payments)"]
    assert r["backup"] and "before-sample-removal-" in r["backup"]
    assert sample_ids(store) == (set(), set())  # kept ones are real now
    assert {store.get_candidate(i)["name"] for i in (by_name["Maya Okafor"], real["id"])}
    assert [p["candidate_name"] for p in store.pipeline(by_title["Data Lead"])] == ["Jordan Pierce"]


def test_untouched_sample_data_goes_and_the_page_says_so(store):
    c = local_client(store)
    c.post("/sample-data")
    r = c.post("/sample-data/remove", headers={"referer": "http://127.0.0.1:8765/settings"}, follow_redirects=False)
    msg = notice(r)
    assert "Removed the sample data: 16 candidates and 2 jobs." in msg and "backup" in msg and "Kept" not in msg
    assert store.count_candidates() == 0 and any(n.startswith("before-sample-removal-") for n in backups())


def test_sample_copy_mentions_backup_and_kept_work(store):
    c = local_client(store)
    c.post("/sample-data")
    page = c.get("/settings").text
    assert "takes a backup first" in page and "Your own data stays" not in page
    assert "Your own data stays" not in c.get("/").text


# --------------------------------------------------------------- 6. delete
def test_secure_delete_is_on(store):
    assert store._conn.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_delete_backs_up_and_names_the_person(store):
    maya = store.add_candidate(name="Maya Okafor")
    c = local_client(store)
    page = c.get(f"/candidates/{maya['id']}").text
    assert "Copies stay in automatic backups until they roll off." in page
    r = c.post(f"/candidates/{maya['id']}/delete", follow_redirects=False)
    assert notice(r) == "Deleted Maya Okafor. This can't be undone."
    assert any(n.startswith("before-delete-") for n in backups()) and store.count_candidates() == 0
    job = store.add_job("Lead")
    c.post(f"/jobs/{job['id']}/delete")
    assert sum(n.startswith("before-delete-") for n in backups()) == 2


def test_settings_deletes_old_backups(store):
    c = local_client(store)
    assert "No backups yet." in c.get("/settings").text
    importers.backup_before_import(store)
    importers.backup_before_import(store, reason="delete")
    page = c.get("/settings").text
    assert "Delete old backups" in page and "are 2 backups" in page
    r = c.post("/settings/backups/delete", follow_redirects=False)
    assert notice(r) == "Deleted 2 backups." and backups() == []


# ---------------------------------------------------------- 7. limits
def test_field_lengths_are_capped(store):
    with pytest.raises(ValueError, match="About is too long"):
        store.add_candidate(name="Bo", about="x" * 5001)
    with pytest.raises(ValueError, match="Name is too long"):
        store.add_candidate(name="B" * 201)
    bo = store.add_candidate(name="Bo", about="x" * 5000)
    with pytest.raises(ValueError, match="skills list is too long"):
        store.update_candidate(bo["id"], tags=[f"skill{i}" for i in range(400)])


# ----------------------------------------------------------- 8. merge
def test_merge_two_people(store):
    keep = store.add_candidate(name="Maya Okafor", email="maya@x.com", tags="go")
    other = store.add_candidate(name="maya okafor", phone="+1 415 555 0100", linkedin_url="linkedin.com/in/maya",
                                tags="rust, Go", current_title="Engineer", about="Payments")
    store.log(other["id"], "note", "Met at a meetup")
    a, b = store.add_job("Backend"), store.add_job("Data")
    store.add_to_pipeline(keep["id"], a["id"])
    store.add_to_pipeline(other["id"], a["id"], "Interviewing")
    store.add_to_pipeline(other["id"], b["id"])
    store.update_candidate(other["id"], work_history=[{"title": "Engineer", "company": "Stripe"}])
    assert [d["id"] for d in store.possible_duplicates(keep["id"])] == [other["id"]]
    merged = store.merge_candidates(keep["id"], other["id"])
    assert merged["email"] == "maya@x.com" and merged["phone"] == "+1 415 555 0100"
    assert merged["linkedin_url"] == "https://linkedin.com/in/maya" and merged["tags"] == ["go", "rust"]
    assert merged["about"] == "Payments" and store.count_candidates() == 1
    stages = {p["job_title"]: p["stage"] for p in store.candidate_pipelines(keep["id"])}
    assert stages == {"Backend": "Interviewing", "Data": "Sourced"}
    assert [w["company"] for w in store.work_history(keep["id"])] == ["Stripe"]
    tl = store.timeline(keep["id"])
    entry = next(t for t in tl if t["kind"] == "change" and t["data"].get("merged"))
    assert entry["body"].startswith(f"Merged in maya okafor (id {other['id']})")
    assert entry["data"]["merged"]["linkedin_url"] == "https://linkedin.com/in/maya" and not entry["undoable"]
    assert any(t["body"] == "Met at a meetup" for t in tl)
    with pytest.raises(ValueError, match="merge can't be undone"):
        store.undo_change(entry["id"])


def test_merge_in_the_app(store):
    keep = store.add_candidate(name="Daniel Kim", email="dk@x.com")
    other = store.add_candidate(name="Daniel Kim", current_title="PM")
    c = local_client(store)
    page = c.get(f"/candidates/{keep['id']}").text
    assert "Same person?" in page and f"/candidates/{keep['id']}/merge/{other['id']}" in page
    confirm = c.get(f"/candidates/{keep['id']}/merge/{other['id']}").text
    assert "Keep this one" in confirm and "Merge in and delete this one" in confirm and "PM" in confirm
    r = c.post(f"/candidates/{keep['id']}/merge/{other['id']}", follow_redirects=False)
    assert notice(r).startswith(f"Merged Daniel Kim (id {other['id']}) into Daniel Kim (id {keep['id']}): filled title") and store.count_candidates() == 1
    assert any(n.startswith("before-merge-") for n in backups())
    assert "Same person?" not in c.get(f"/candidates/{keep['id']}").text
    for path in ("/", f"/candidates/{keep['id']}", "/settings"):
        body = c.get(path).text
        assert "—" not in body and "–" not in body


def test_the_ai_has_no_merge_or_delete_tool(store):
    names = {t.name for t in anyio.run(build_server(store).list_tools)}
    assert not any("merge" in n or "delete" in n for n in names)


# ------------------------------------------------- 9. undo AI stage moves
def test_ai_stage_moves_and_adds_can_be_undone(store):
    server = build_server(store)
    lee = store.add_candidate(name="Lee Chen")
    job = store.add_job("Backend")
    call(server, "add_to_pipeline", candidate_id=lee["id"], job_id=job["id"])
    call(server, "move_stage", candidate_id=lee["id"], job_id=job["id"], stage="Screening")
    moves = [t for t in store.timeline(lee["id"]) if t["kind"] == "stage"]
    assert [t["body"] for t in moves] == ["Screening", "Sourced"] and all(t["undoable"] for t in moves)
    assert moves[0]["source"] == "AI app"
    with pytest.raises(ValueError, match="moved again"):
        store.undo_change(moves[1]["id"])  # the add, but they've moved on since
    c = local_client(store)
    assert c.get(f"/candidates/{lee['id']}").text.count(">Undo</button>") == 2
    r = c.post(f"/candidates/{lee['id']}/undo/{moves[0]['id']}", follow_redirects=False)
    assert "Undid" in notice(r) and store.candidate_pipelines(lee["id"])[0]["stage"] == "Sourced"
    store.undo_change(moves[1]["id"])
    assert store.candidate_pipelines(lee["id"]) == []
    with pytest.raises(ValueError, match="already undone"):
        store.undo_change(moves[1]["id"])
    # The recruiter's own moves stay as they were (no Undo).
    store.add_to_pipeline(lee["id"], job["id"])
    store.move_stage(lee["id"], job["id"], "Offer")
    assert not next(t for t in store.timeline(lee["id"]) if t["kind"] == "stage")["undoable"]
