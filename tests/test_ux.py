"""App window UX: Today grouping, follow-up dates, sample data, job delete, status update, small fixes."""
from datetime import date, datetime, timedelta, timezone

import pytest

from talent import actions
from talent.sample_data import load_sample_data, remove_sample_data, sample_ids
from talent.store import NotFound
from conftest import local_client

ORIGIN = {"origin": "http://127.0.0.1:8765"}


def client(store):
    return local_client(store)


def _soon(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0)


# ------------------------------------------------------------------ Today
def test_thin_jobs_fold_into_one_row_with_company_and_threshold(store):
    store.set_settings(thin_min="4")
    for t in ("Backend", "Data Lead", "Designer"):
        store.add_job(t, company="Acme Payments")
    rows = [i for i in actions.action_items(store)["items"] if i["kind"] == "thin"]
    assert len(rows) == 1
    assert rows[0]["title"] == "3 jobs have thin pipelines"
    assert "Backend, Acme Payments" in rows[0]["detail"] and "Aim for at least 4 active candidates." in rows[0]["detail"]
    html = client(store).get("/").text
    assert "3 jobs have thin pipelines" in html and "Needs more candidates" in html


def test_single_thin_job_detail_copy(store):
    store.add_job("Senior Backend Engineer", company="Acme Payments")
    row = next(i for i in actions.action_items(store)["items"] if i["kind"] == "thin")
    assert row["title"] == "Senior Backend Engineer has no active candidates"
    assert row["detail"] == "Acme Payments. Aim for at least 5 active candidates."


def test_stale_profiles_fold_into_one_row_with_real_count(store):
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    for n in range(260):  # more than any query limit used to allow
        c = store.add_candidate(name=f"Old {n}")
        store._conn.execute("UPDATE candidates SET updated_at = ? WHERE id = ?", (old, c["id"]))
    store._conn.commit()
    res = actions.action_items(store)
    rows = [i for i in res["items"] if i["kind"] == "stale"]
    assert len(rows) == 1 and rows[0]["title"] == "260 profiles may be out of date"
    assert res["stale_total"] == 260
    store.snooze(rows[0]["key"], 7)
    after = actions.action_items(store)
    assert not [i for i in after["items"] if i["kind"] == "stale"] and after["hidden"] == 1


def test_interviews_hidden_for_closed_jobs_and_closed_applications(store):
    a, b, c = (store.add_candidate(name=n) for n in ("Ana", "Ben", "Cy"))
    job = store.add_job("Backend", company="Acme Payments")
    closed = store.add_job("Old role", company="Acme Payments")
    for cand in (a, b):
        store.add_to_pipeline(cand["id"], job["id"])
    store.add_to_pipeline(c["id"], closed["id"])
    store.move_stage(b["id"], job["id"], "Rejected")
    store.update_job(closed["id"], status="closed")
    for cand, j in ((a, job), (b, job), (c, closed)):
        store.log(cand["id"], "interview", "Round 1", at=_soon(), job_id=j["id"])
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "interview"]
    assert [i["title"] for i in items] == ["Interview with Ana"]
    assert items[0]["detail"].startswith("Backend, Acme Payments")


def test_today_order_interviews_then_followups(store):
    load_sample_data(store)
    kinds = [i["kind"] for i in actions.action_items(store)["items"]]
    assert kinds[:2] == ["interview", "suggestion"]
    assert kinds.count("thin") == 1 and kinds.count("stale") == 1


# --------------------------------------------------------------- follow-ups
def test_follow_up_date_on_log_form_shows_from_that_day(store):
    cand = store.add_candidate(name="Ana Ruiz")
    c = client(store)
    later = (date.today() + timedelta(days=5)).isoformat()
    r = c.post(f"/candidates/{cand['id']}/log", data={"kind": "call", "body": "Intro call", "follow_up": later},
               follow_redirects=False)
    assert r.status_code == 303 and "notice=" in r.headers["location"]
    assert store.timeline(cand["id"])[0]["body"] == "Intro call"
    res = actions.action_items(store)
    assert not [i for i in res["items"] if i["kind"] == "suggestion"] and res["scheduled"] == 1
    assert "scheduled for later" in c.get("/").text
    on_day = datetime.now(timezone.utc) + timedelta(days=6)
    items = [i for i in actions.action_items(store, now=on_day)["items"] if i["kind"] == "suggestion"]
    assert [i["title"] for i in items] == ["Follow up with Ana Ruiz"]
    assert "AI app" not in items[0]["detail"]
    # Done clears the follow-up date too.
    c.post(f"/actions/suggestions/{items[0]['suggestion_id']}/done")
    assert not [k for k in store.settings() if k.startswith("followup:")]


def test_follow_up_alone_and_bad_date(store):
    cand = store.add_candidate(name="Ben")
    c = client(store)
    c.post(f"/candidates/{cand['id']}/log", data={"kind": "note", "body": "", "follow_up": date.today().isoformat()})
    assert [i["title"] for i in actions.action_items(store)["items"] if i["kind"] == "suggestion"] == ["Follow up with Ben"]
    assert not store.timeline(cand["id"])  # no empty note logged
    r = c.post(f"/candidates/{cand['id']}/log", data={"kind": "call", "body": "x", "follow_up": "soon"},
               follow_redirects=False)
    assert "error=" in r.headers["location"]
    page = c.get(f"/candidates/{cand['id']}").text
    assert 'name="follow_up"' in page and "Follow up on" in page


# ------------------------------------------------------------- sample data
def test_sample_data_badges_and_remove(store):
    real = store.add_candidate(name="Real Person")
    real_job = store.add_job("Real job", company="Mine")
    c = client(store)
    c.post("/sample-data")
    people, jobs = sample_ids(store)
    assert len(people) == 16 and len(jobs) == 2 and real["id"] not in people and real_job["id"] not in jobs
    assert "badge sample" in c.get("/").text
    job_id = next(iter(jobs))
    assert "badge sample" in c.get(f"/jobs/{job_id}").text and "badge sample" in c.get("/jobs").text
    settings = c.get("/settings").text
    assert "Remove sample data" in settings and "16 sample candidates" in settings
    r = c.post("/sample-data/remove", headers={"referer": "http://127.0.0.1:8765/settings?saved=1"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings?notice=")
    assert "saved=" not in r.headers["location"]  # old messages don't come back
    assert store.count_candidates() == 1 and [j["id"] for j in store.list_jobs()] == [real_job["id"]]
    assert sample_ids(store) == (set(), set())
    assert "Removed the sample data" in c.get(r.headers["location"]).text
    again = remove_sample_data(store)
    assert (again["people"], again["jobs"], again["kept_people"]) == (0, 0, [])


def test_welcome_stays_until_a_real_candidate_and_search_hidden_when_empty(store):
    c = client(store)
    empty = c.get("/").text
    assert "Welcome to Talent Search" in empty and 'role="search"' not in empty
    load_sample_data(store)
    with_sample = c.get("/").text
    assert "Add your first real candidate" in with_sample and "Remove sample data" in with_sample
    assert 'role="search"' in with_sample
    store.add_candidate(name="First Real")
    assert "Add your first real candidate" not in c.get("/").text


# ---------------------------------------------------------------- jobs
def test_delete_job_keeps_candidates(store):
    cand = store.add_candidate(name="Stays")
    job = store.add_job("Gone", company="Acme")
    store.add_to_pipeline(cand["id"], job["id"])
    store.add_suggestion("Ping Stays", candidate_id=cand["id"], job_id=job["id"])
    c = client(store)
    assert "Delete job" in c.get(f"/jobs/{job['id']}").text
    r = c.post(f"/jobs/{job['id']}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs?notice=")
    assert store.list_jobs() == [] and store.get_candidate(cand["id"])["name"] == "Stays"
    assert store.candidate_pipelines(cand["id"]) == []
    assert [s["text"] for s in store.open_suggestions()] == ["Ping Stays"]
    assert "candidates are still saved" in c.get(r.headers["location"]).text
    assert c.post(f"/jobs/{job['id']}/delete").status_code == 404
    with pytest.raises(NotFound):
        store.delete_job(job["id"])


def test_delete_job_is_not_an_mcp_tool():
    import inspect

    from talent import mcp_server
    assert "delete_job" not in inspect.getsource(mcp_server)


def test_status_update_text_and_board_next_interview(store):
    maya = store.add_candidate(name="Maya Okafor")
    ken = store.add_candidate(name="Kenji Watanabe")
    job = store.add_job("Senior Backend Engineer", company="Acme Payments", location="Austin, TX")
    store.add_to_pipeline(maya["id"], job["id"])
    store.add_to_pipeline(ken["id"], job["id"])
    store.move_stage(maya["id"], job["id"], "Interviewing")
    when = _soon(3)
    store.log(maya["id"], "interview", "Systems design", at=when, job_id=job["id"])
    text = actions.job_status_text(store, job["id"])
    assert text.splitlines()[0] == "Senior Backend Engineer, Acme Payments (Austin, TX)"
    assert "Interviewing (1): Maya Okafor (today)" in text and "Sourced (1): Kenji Watanabe (today)" in text
    assert "Offer" not in text  # empty stages left out
    assert "Next interviews:" in text and actions.local_time(when.isoformat(), "%a %b %-d") in text
    assert "id" not in text.replace("Kenji", "").lower().split()  # no ids
    c = client(store)
    board = c.get(f"/jobs/{job['id']}?view=board").text
    assert "Copy status update" in board and "next-iv" in board
    assert "Next interview" in c.get(f"/jobs/{job['id']}?view=table").text


# ------------------------------------------------------------- small fixes
def test_advance_and_snooze_errors_flash_instead_of_failing(store):
    c = client(store)
    ref = {"referer": "http://127.0.0.1:8765/"}
    r = c.post("/actions/advance", data={"candidate_id": "1", "job_id": "x", "stage": "Offer"}, headers=ref,
               follow_redirects=False)
    assert r.status_code == 303 and "flash_error=" in r.headers["location"]
    cand = store.add_candidate(name="Ana")
    job = store.add_job("Role")
    r = c.post("/actions/advance", data={"candidate_id": cand["id"], "job_id": job["id"], "stage": "Offer"},
               headers=ref)
    assert r.status_code == 200 and "isn&#39;t in this job" in r.text
    store.add_to_pipeline(cand["id"], job["id"])
    r = c.post("/actions/advance", data={"candidate_id": cand["id"], "job_id": job["id"], "stage": "Nope"}, headers=ref)
    assert "No stage called" in r.text
    r = c.post("/actions/advance", data={"candidate_id": cand["id"], "job_id": job["id"], "stage": "Offer"}, headers=ref)
    assert "Moved Ana to Offer" in r.text and store.pipeline(job["id"])[0]["stage"] == "Offer"
    assert "flash_error=" in c.post("/actions/snooze", data={"key": "k", "days": "soon"}, headers=ref,
                                    follow_redirects=False).headers["location"]
    assert c.post("/actions/suggestions/999/done", headers=ref).status_code == 200


def test_settings_inline_error_for_bad_thresholds(store):
    c = client(store)
    r = c.post("/settings", data={"stuck_days": "abc", "thin_min": "-3", "stale_days": "90"})
    assert r.status_code == 400 and r.text.count("Use a whole number from 1 to 9999.") == 2
    assert "Nothing was saved" in r.text and 'value="abc"' in r.text
    assert store.setting("stuck_days") == "7" and store.setting("stale_days") == "180"
    r = c.post("/settings", data={"stuck_days": "10", "stale_days": "90"}, follow_redirects=False)
    assert r.status_code == 303 and store.setting("stuck_days") == "10" and store.setting("thin_min") == "5"


def test_timeline_interview_time_and_job_company(store):
    cand = store.add_candidate(name="Ana")
    a = store.add_job("Engineer", company="Acme")
    b = store.add_job("Engineer", company="Globex")
    when = _soon(1).replace(hour=15, minute=30)
    store.log(cand["id"], "interview", "Round 1", at=when)
    store.add_to_pipeline(cand["id"], a["id"])
    store.add_to_pipeline(cand["id"], b["id"])
    c = client(store)
    page = c.get(f"/candidates/{cand['id']}").text
    assert actions.local_time(when.isoformat(), "%-I:%M %p") in page
    assert "Engineer, Acme</option>" in page and "Engineer, Globex</option>" in page
    jobs = c.get("/jobs").text
    assert ", Acme" in jobs and ", Globex" in jobs


def test_new_pages_have_no_dashes(store):
    load_sample_data(store)
    c = client(store)
    job = store.list_jobs()[0]["id"]
    cid = store.recent_candidates(1)[0]["id"]
    for path in ["/", "/jobs", f"/jobs/{job}", f"/jobs/{job}?view=table", "/settings", f"/candidates/{cid}"]:
        text = c.get(path).text
        assert "—" not in text and " – " not in text, path
    assert "—" not in actions.job_status_text(store, job)
