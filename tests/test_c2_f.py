"""Cycle 2, fixer F: re-review bugs and cheap wins."""
import csv
import io
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from talent import actions, importers
from talent.mcp_server import build_server
from talent.sample_data import load_sample_data, remove_sample_data, sample_ids
from talent.store import Store

from conftest import flash_of, local_client
from test_mcp import call, structured, text


def _at(days=2, hours=0):
    return (datetime.now(timezone.utc) + timedelta(days=days, hours=hours)).replace(second=0, microsecond=0)


def _msg(r, key="notice"):
    return flash_of(r, key)


def _run(store, csv_text):
    header, rows = importers.read_csv(csv_text)
    return importers.import_rows(store, header, rows, importers.guess_mapping(header, rows), backup=False)


# ---------------------------------------------------------- 1. erasure
def test_deleting_someone_empties_the_wal(tmp_path):
    s = Store(tmp_path / "t.db")
    wal = tmp_path / "t.db-wal"
    mara = s.add_candidate(name="Mara Quillfeather", about="Zanzibar-quokka marker text")
    assert wal.exists() and wal.stat().st_size > 0
    s.delete_candidate(mara["id"])
    assert wal.stat().st_size == 0
    a = s.add_candidate(name="Mara Q", email="mq@x.com")
    b = s.add_candidate(name="Mara Q", about="Second quokka copy")
    s.merge_candidates(a["id"], b["id"])
    assert wal.stat().st_size == 0
    job = s.add_job("Quokka wrangler")
    s.delete_job(job["id"])
    assert wal.stat().st_size == 0
    s.add_candidate(name="Last One")
    s.close()
    assert not wal.exists() or wal.stat().st_size == 0
    assert b"zanzibar" not in (tmp_path / "t.db").read_bytes().lower()


def test_delete_old_backups_checkpoints_too(store, client):
    r = client.post("/settings/backups/delete", follow_redirects=False)
    assert r.status_code == 303
    wal = store.path + "-wal"
    import os
    assert not os.path.exists(wal) or os.path.getsize(wal) == 0


# ------------------------------------------------- 2. AI identity guard
def test_one_ai_identity_change_per_person_per_ten_minutes(store):
    lee = store.add_candidate(name="Lee Chen", email="lee@x.com", phone="555 0100")
    server = build_server(store)
    with pytest.raises(Exception) as exc:
        call(server, "update_candidate", candidate_id=lee["id"], email="e@evil.example", phone="555 9999")
    assert "one at a time" not in str(exc.value) and "Ask the recruiter to confirm" in str(exc.value)
    call(server, "update_candidate", candidate_id=lee["id"], email="harvest@evil.example")
    with pytest.raises(Exception, match="changed this person's email a few minutes ago. Ask the recruiter"):
        call(server, "update_candidate", candidate_id=lee["id"], phone="555 9999")
    assert store.get_candidate(lee["id"])["phone"] == "555 0100"
    # Other fields still go through, and so does another person.
    call(server, "update_candidate", candidate_id=lee["id"], current_title="Staff Engineer")
    bo = store.add_candidate(name="Bo Li", email="bo@x.com")
    call(server, "update_candidate", candidate_id=bo["id"], phone="555 1234")
    # An undone change doesn't count.
    entry = next(t for t in store.timeline(lee["id"]) if t["kind"] == "change" and "email" in t["data"]["new"])
    store.undo_change(entry["id"])
    call(server, "update_candidate", candidate_id=lee["id"], phone="555 9999")
    # Nor does one older than 10 minutes.
    old = (datetime.now(timezone.utc) - timedelta(minutes=11)).replace(microsecond=0).isoformat()
    store._conn.execute("UPDATE timeline SET created_at = ?, at = ? WHERE kind = 'change'", (old, old))
    store._conn.commit()
    call(server, "update_candidate", candidate_id=lee["id"], linkedin_url="https://linkedin.com/in/leechen")


# ------------------------------------------------------- 3. interviews
def test_no_outcome_for_a_future_interview(store):
    job = store.add_job("Backend", company="Acme")
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    store.add_to_pipeline(ana, job["id"], "Interviewing")
    iv = store.log(ana, "interview", "Screen", at=_at(4), job_id=job["id"])
    with pytest.raises(ValueError, match="hasn't happened yet"):
        store.update_interview(iv["id"], outcome="passed")
    with pytest.raises(Exception, match="hasn't happened yet"):
        call(build_server(store), "update_interview", interview_id=iv["id"], outcome="passed")
    c = local_client(store)
    r = c.post(f"/candidates/{ana}/interviews/{iv['id']}", data={"op": "outcome", "outcome": "passed"},
               follow_redirects=False)
    assert "hasn't happened yet" in _msg(r, "error")
    assert store.get_interview(iv["id"])["status"] == "scheduled"
    assert [i for i in actions.action_items(store)["items"] if i["kind"] == "interview"]
    past = store.log(ana, "interview", "Intro", at=_at(-1), job_id=job["id"])
    assert store.update_interview(past["id"], outcome="passed")["status"] == "passed"


def test_reschedule_reason_is_its_own_field(store):
    job = store.add_job("Backend", company="Acme")
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    iv = store.log(ana, "interview", "Screen", at=_at(3), job_id=job["id"])
    out = structured(call(build_server(store), "update_interview", interview_id=iv["id"],
                          when=_at(5).isoformat(), note="Dana out sick"))
    assert out["interview"]["reschedule_note"] == "Dana out sick" and "outcome_note" not in out["interview"]
    c = local_client(store)
    page = c.get(f"/candidates/{ana}").text
    assert "Reason: Dana out sick" in page and "How it went: Dana out sick" not in page
    assert 'name="reason"' in page and "Reason (optional)" in page
    new = _at(6).astimezone().strftime("%Y-%m-%dT%H:%M")
    r = c.post(f"/candidates/{ana}/interviews/{iv['id']}",
               data={"op": "reschedule", "at": new, "reason": "Room clash"}, follow_redirects=False)
    assert "Moved the interview" in _msg(r)
    assert store.get_interview(iv["id"])["data"]["reschedule_note"] == "Room clash"
    assert "Reason: Room clash" in c.get(f"/candidates/{ana}").text


# --------------------------------------------------- 4. web stage moves
def test_web_stage_moves_can_be_undone(store):
    job = store.add_job("Backend", company="Acme")
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    store.add_to_pipeline(ana, job["id"], "Screening")
    c = local_client(store)
    c.post(f"/jobs/{job['id']}/move", data={"candidate_id": ana, "stage": "Withdrawn"})
    entry = next(t for t in store.timeline(ana) if t["kind"] == "stage" and t["body"] == "Withdrawn")
    assert entry["undoable"] and entry["data"]["pipeline"] == {"job_id": job["id"], "old": "Screening",
                                                               "new": "Withdrawn"}
    page = c.get(f"/candidates/{ana}").text
    assert f"/candidates/{ana}/undo/{entry['id']}" in page
    c.post(f"/candidates/{ana}/undo/{entry['id']}")
    assert store.candidate_pipelines(ana)[0]["stage"] == "Screening"
    # The Today "Move to" button records its move the same way.
    c.post("/actions/advance", data={"candidate_id": ana, "job_id": job["id"], "stage": "Interviewing"})
    assert next(t for t in store.timeline(ana) if t["kind"] == "stage")["undoable"]


# ------------------------------------------------------------- 5. merge
def test_merge_records_values_it_did_not_keep(store):
    keep = store.add_candidate(name="Jose Alvarez", email="j@x.com", linkedin_url="https://linkedin.com/in/jalvarez",
                               current_title="SRE")
    other = store.add_candidate(name="Jose Alvarez", linkedin_url="https://linkedin.com/in/jalvarez2",
                                current_title="Senior SRE", phone="555 0100")
    c = local_client(store)
    confirm = c.get(f"/candidates/{keep['id']}/merge/{other['id']}").text
    assert f"(id {keep['id']})" in confirm and f"(id {other['id']})" in confirm
    r = c.post(f"/candidates/{keep['id']}/merge/{other['id']}", follow_redirects=False)
    n = _msg(r)
    assert f"(id {other['id']})" in n and f"(id {keep['id']})" in n
    assert "jalvarez2" in n and "title: Senior SRE" in n
    kept = store.get_candidate(keep["id"])
    assert kept["current_title"] == "SRE" and kept["phone"] == "555 0100" and "jalvarez2" not in kept["linkedin_url"]
    entry = next(t for t in store.timeline(keep["id"]) if t["kind"] == "change" and t["data"].get("merged"))
    assert "jalvarez2" in entry["body"] and "title: Senior SRE" in entry["body"]
    assert entry["data"]["merged"]["dropped"]["current_title"] == "Senior SRE"
    assert "jalvarez2" in c.get(f"/candidates/{keep['id']}").text


# ------------------------------------------------------------ 6. export
def test_export_notes_carry_every_kind_and_round_trip(store, tmp_path):
    ana = store.add_candidate(name="Ana Diaz", email="ana@x.com")
    job = store.add_job("Backend", "Acme")
    store.add_to_pipeline(ana["id"], job["id"])
    store.log(ana["id"], "note", "Plain note", at="2026-01-01")
    store.log(ana["id"], "call", "Left a voicemail", at="2026-01-02")
    store.log(ana["id"], "email", "Sent the JD", at="2026-01-03")
    store.log(ana["id"], "meeting", "Coffee", at="2026-01-04")
    store.log(ana["id"], "interview", "Onsite | panel", at="2026-01-05T15:00:00+00:00")
    out = list(csv.DictReader(io.StringIO(importers.export_csv(store))))
    assert out[0]["Notes"] == ("interview: Onsite \\| panel | meeting: Coffee | email: Sent the JD | "
                               "call: Left a voicemail | Plain note")
    text_csv = importers.export_csv(store)
    assert _run(store, text_csv)["notes"] == 0  # back into the same database: nothing doubles
    fresh = Store(tmp_path / "fresh.db")
    assert _run(fresh, text_csv)["notes"] == 5
    a = fresh.find_duplicate("ana@x.com")
    assert "call: Left a voicemail" in [t["body"] for t in fresh.timeline(a["id"]) if t["kind"] == "note"]
    assert not [t for t in fresh.timeline(a["id"]) if t["kind"] == "interview"]  # no made-up schedule
    again = importers.export_csv(fresh)
    assert _run(fresh, again)["notes"] == 0 and _run(fresh, text_csv)["notes"] == 0
    fresh.close()


# ------------------------------------------------------- 7. sample data
def test_kept_sample_people_lose_the_sample_entries(store):
    load_sample_data(store)
    people, _ = sample_ids(store)
    maya = next(i for i in people if store.get_candidate(i)["name"] == "Maya Okafor")
    store.log(maya, "call", "Real call about Austin", source="App")
    remove_sample_data(store)
    tl = store.timeline(maya)
    assert [t["body"] for t in tl if t["kind"] == "call"] == ["Real call about Austin"]
    assert not [t for t in tl if t["kind"] == "interview" or t["source"] == "Sample data"]
    assert not [i for i in actions.action_items(store)["items"] if i["kind"] == "interview"]
    assert store.get_candidate(maya)["source"] is None


# ---------------------------------------------------------------- 8. MCP
def test_unknown_mcp_arguments_are_named(store):
    server = build_server(store)
    ana = store.add_candidate(name="Ana Ruiz")
    job = store.add_job("Backend")
    with pytest.raises(Exception) as exc:
        call(server, "add_suggestion", text="Call Ana", candidate_id=ana["id"], job=job["id"])
    msg = str(exc.value)
    assert "doesn't take job" in msg and "Did you mean job_id?" in msg and "It accepts: text" in msg
    assert not store.open_suggestions()
    with pytest.raises(Exception, match="doesn't take headline"):
        call(server, "update_candidate", candidate_id=ana["id"], headline="Staff")


def test_title_and_company_aliases_and_ignored_fields(store):
    server = build_server(store)
    res = call(server, "save_candidate", name="Priya Raman", email="p@x.com", title="Staff Engineer",
               company="Stripe", headline="Builds ledgers", skills=["Go"])
    out = structured(res)
    c = store.find_duplicate("p@x.com")
    assert (c["current_title"], c["current_company"], c["tags"]) == ("Staff Engineer", "Stripe", ["Go"])
    assert out["ignored_fields"] == ["headline"] and "headline" in text(res)
    out = structured(call(server, "save_candidate", name="Priya Raman", email="p@x.com"))
    assert "ignored_fields" not in out
    call(server, "update_candidate", candidate_id=c["id"], title="Principal Engineer", company="Square")
    c = store.get_candidate(c["id"])
    assert (c["current_title"], c["current_company"]) == ("Principal Engineer", "Square")


# ------------------------------------------------------- 9. stage names
def test_stage_names_are_at_most_60_characters(store):
    long = "S" * 61
    job = store.add_job("Backend")
    for do in (lambda: store.add_stage(job["id"], long), lambda: store.rename_stage(job["id"], "Sourced", long),
               lambda: store.add_job("Other", stages=["Sourced", long])):
        with pytest.raises(ValueError, match="up to 60 characters"):
            do()
    server = build_server(store)
    with pytest.raises(Exception, match="up to 60 characters"):
        call(server, "create_job", title="Third", stages=[long])
    with pytest.raises(Exception, match="up to 60 characters"):
        call(server, "edit_job_stages", job_id=job["id"], add=[long])
    assert store.add_stage(job["id"], "S" * 60)
    r = local_client(store).post(f"/jobs/{job['id']}/stages", data={"op": "add", "new_name": long})
    assert "up to 60 characters" in r.text


# ----------------------------------------------------- 10. Leave out
def test_leave_out_is_remembered_per_job(store):
    job = store.add_job("Backend", company="Globex", stages=["Sourced", "Screen", "Rejected", "Hired"])
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    lena = store.add_candidate(name="Lena Park")["id"]
    store.add_to_pipeline(ana, job["id"], "Screen")
    store.add_to_pipeline(lena, job["id"], "Rejected")
    c = local_client(store)
    r = c.post(f"/jobs/{job['id']}/omit", data={"omit": ["Rejected", "nope"]}, follow_redirects=False)
    assert "leave out Rejected" in _msg(r) and actions.saved_omit(store, job["id"]) == ["Rejected"]
    page = c.get(f"/jobs/{job['id']}").text
    assert 'value="Rejected" checked' in page and "Lena Park" not in actions.job_status_text(store, job["id"])
    assert "Lena Park" not in actions.status_digest(store, "Globex")
    assert "Lena Park" not in c.get("/jobs").text
    server = build_server(store)
    assert "Lena Park" not in text(call(server, "status_digest", company="Globex"))
    assert "Lena Park" not in text(call(server, "job_pipeline", job_id=job["id"]))
    assert "Lena Park" in text(call(server, "job_pipeline", job_id=job["id"], omit_stages=[]))
    c.post(f"/jobs/{job['id']}/omit", data={})
    assert actions.saved_omit(store, job["id"]) == [] and "Lena Park" in actions.status_digest(store, "Globex")


# -------------------------------------------------------------- 11. Today
def test_future_todos_are_listed_with_their_day_and_overdue_says_it_once(store):
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    actions.add_followup(store, "Check offer signed", date.today() - timedelta(days=2), candidate_id=ana)
    soon = date.today() + timedelta(days=5)
    actions.add_followup(store, "Ping Ana about start date", soon, candidate_id=ana)
    actions.add_followup(store, "Quarterly check-in", date.today() + timedelta(days=40))
    res = actions.action_items(store)
    later = [i for i in res["next_week"] if i["kind"] == "suggestion"]
    assert [i["title"] for i in later] == ["Ping Ana about start date"]
    assert later[0]["due_text"].startswith("Due ") and res["scheduled"] == 2 and res["scheduled_later"] == 1
    home = local_client(store).get("/").text
    assert "Ping Ana about start date" in home and "1 more follow-up scheduled for later" in home
    assert '<span class="badge overdue">Overdue</span> <span class="fine">due ' in home
    assert "Overdue, due" not in home
    assert "Ping Ana about start date" in text(call(build_server(store), "list_action_items"))


# -------------------------------------------------------- 12. hire word
def test_rollup_uses_the_jobs_hire_word(store):
    store.add_job("Head of Data", company="Northwind", stages=["Sourced", "Submitted", "Placed"])
    assert "0 of 1 placed." in actions.status_digest(store, "Northwind")
    assert actions.company_rollup(store)[0]["word"] == "placed"
    assert "<th>Placed</th>" in local_client(store).get("/jobs").text
    store.add_job("Backend", company="Northwind")
    assert "0 of 2 hired." in actions.status_digest(store, "Northwind")


# -------------------------------------------------------- 13. dry run
def test_dry_run_speaks_in_the_future_and_the_button_says_what_it_does(store):
    store.add_candidate(name="Maya Okafor", email="maya@x.com")
    store.add_candidate(name="Lee Chen", email="lee@x.com")
    csv_text = ("Name,Email,Title\nLee Chen,lee@x.com,Staff\nMaya Smith,maya@x.com,PM\n"
                "Ben Cole,ben@x.com,PM\nCy Ng,cy@x.com,PM\n")
    header, rows = importers.read_csv(csv_text)
    preview = importers.preview_import(store, header, rows, importers.guess_mapping(header, rows))
    assert any("so it will be left alone" in s for s in preview["skipped"])
    assert not any("was left alone" in s for s in preview["skipped"])
    assert importers.button_label(preview, 4) == "Import 2 new people and update 1"
    assert importers.button_label({"added": 1, "updated": 0}, 1) == "Import 1 new person"
    assert importers.button_label({"added": 0, "updated": 2}, 2) == "Update 2 people"
    assert importers.button_label(None, 7) == "Import 7 rows"
    page = local_client(store).post("/import", files={"file": ("p.csv", csv_text.encode())}).text
    assert "Import 2 new people and update 1" in page and "will be left alone" in page
    real = _run(store, csv_text)
    assert any("so it was left alone" in s for s in real["skipped"])
