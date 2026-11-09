"""Cycle 6 re-review, fixer H: the feedback chase agrees with Decide, offers lead Today (a stage nudge, no stale
moves, a hire whose offer still reads out, answers due a few days ahead, imported offer rows on one line), a held
hire keeps its people in the update, in-house wording, and the small ones (plurals, name casing, openings left,
Let them know, Settings that change only what's sent)."""
from datetime import date, datetime, timedelta, timezone

import pytest
import anyio
from mcp.types import CallToolResult

from talent import actions, decide, interviews, offers, reports
from talent.mcp_server import build_server
from talent.store import as_local

from conftest import flash_of, local_client


@pytest.fixture(autouse=True)
def _trusted_apps_may_hire(store):
    """Cycle 10 wave 2: Hired and Close job wait for review by default. These tests are about what happens after an
    AI app hires or closes, so they turn on the Settings switch that lets a trusted app do it."""
    store.set_settings(ai_trusted_hired="1")

INHOUSE = list(actions.STAGE_SETS["inhouse"][1])


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def data(res) -> dict:
    return res.structuredContent if isinstance(res, CallToolResult) else res[1].get("result", res[1])


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()


def _job(store, name="Tomas Okafor", stage="Interviews", **kw):
    job = store.add_job("Senior Backend Engineer", company="Lumen Health", stages=INHOUSE, **kw)
    cid = store.add_candidate(name=name)["id"]
    store.add_to_pipeline(cid, job["id"], stage)
    return job["id"], cid


def _items(store, kind=None):
    return [i for i in actions.action_items(store)["items"] if kind is None or i["kind"] == kind]


# ------------------------------------------------- 1. the chase agrees with Decide
def test_list_feedback_owed_lists_fresh_sessions_as_not_chased_yet(store):
    jid, cid = _job(store)
    store.log(cid, "interview", "Panel", at=_ago(17), job_id=jid, interviewer="Dana Whitfield")
    assert _items(store, "feedback_owed") == []  # Today still waits a day
    d = decide.finalists(store, jid)
    assert d["people"][0]["owed"] == ["Dana Whitfield"]
    res = call(build_server(store), "list_feedback_owed")
    out = text(res)
    assert "Dana Whitfield owes feedback on Panel" in out and "not chased yet" in out
    assert "last 14 days" in out and "24 hours" in out
    assert data(res)["loops"][0]["owed"][0]["chased"] is False


def test_interviews_page_drops_owed_on_a_closed_or_hired_req(store):
    jid, cid = _job(store)
    store.log(cid, "interview", "Panel", at=_ago(30), job_id=jid, interviewer="Alex Ruiz")
    day = as_local(datetime.now(timezone.utc) - timedelta(hours=30)).date()
    day -= timedelta(days=day.weekday())  # its Monday: week() moves a weekend day on to the next week
    assert interviews.week(store, day)["owed"][0]["feedback"] == ["Alex Ruiz"]
    store.move_stage(cid, jid, "Hired")
    w = interviews.week(store, day)
    assert all(not o["feedback"] for o in w["owed"])
    assert "Owed: Alex Ruiz" not in local_client(store).get(f"/interviews?week={day.isoformat()}").text
    store.move_stage(cid, jid, "Offer")
    assert interviews.week(store, day)["owed"][0]["feedback"] == ["Alex Ruiz"]
    store.update_job(jid, status="closed")
    assert all(not o["feedback"] for o in interviews.week(store, day)["owed"])


# ------------------------------------------------- 2. offers lead Today
def test_record_offer_nudges_a_move_to_the_offer_stage(store):
    jid, cid = _job(store)
    res = call(build_server(store), "record_offer", candidate_id=cid, job_id=jid, salary="$180k", status="out")
    assert data(res)["suggested_step"] == {"tool": "move_stage", "candidate_id": cid, "job_id": jid, "stage": "Offer"}
    assert 'stage="Offer"' in text(res)
    store.move_stage(cid, jid, "Offer")
    res = call(build_server(store), "record_offer", candidate_id=cid, job_id=jid, bonus="10%")
    assert "suggested_step" not in data(res) and "nudge" not in data(res)


def test_a_declined_offer_leaves_no_stale_interview_move(store):
    jid, cid = _job(store)
    iv = store.log(cid, "interview", "Technical", at=_ago(5), job_id=jid, interviewer="Sam Rivera")["id"]
    store.update_interview(iv, outcome="yes")
    assert any(i["candidate_id"] == cid for i in _items(store, "interview_result"))
    offers.record_answer(store, cid, jid, "declined", "Took another offer")
    kinds = [i["kind"] for i in _items(store) if i.get("candidate_id") == cid]
    assert "interview_result" not in kinds and "offer" in kinds


def test_a_hire_whose_offer_still_reads_out_is_flagged(store):
    jid, cid = _job(store, stage="Offer")
    offers.save(store, cid, jid, status="out", answer_by=(date.today() + timedelta(days=20)).isoformat())
    assert not [i for i in _items(store, "offer") if i["key"].startswith("offer_hired")]
    store.move_stage(cid, jid, "Hired")
    item = next(i for i in _items(store, "offer") if i["key"].startswith("offer_hired"))
    assert "Mark the offer accepted?" in item["title"] and item["href"] == offers.href(cid, jid)
    offers.record_answer(store, cid, jid, "accepted")
    assert not [i for i in _items(store, "offer") if i["key"].startswith("offer_hired")]


def test_an_answer_due_in_three_days_shows_on_today(store):
    jid, cid = _job(store, stage="Offer")
    today = as_local(datetime.now(timezone.utc)).date()
    offers.save(store, cid, jid, status="out", answer_by=(today + timedelta(days=5)).isoformat())
    assert not [i for i in _items(store, "offer") if i["key"].startswith("offer_due")]
    offers.save(store, cid, jid, answer_by=(today + timedelta(days=3)).isoformat())
    assert [i for i in _items(store, "offer") if i["key"].startswith("offer_due")]


# ------------------------------------------------- 3. imported offer rows fold into one line
def test_imported_offer_stage_rows_fold_into_one_line(store):
    job = store.add_job("Data Engineer", company="Acme", stages=INHOUSE)
    ids = [store.add_candidate(name=f"Person {i}")["id"] for i in range(6)]
    for cid in ids:
        store.add_to_pipeline(cid, job["id"], "Offer")
    with store._lock, store._conn:
        store._conn.execute("UPDATE timeline SET source = 'CSV import' WHERE kind = 'stage' AND candidate_id IN "
                            f"({','.join('?' * 5)})", ids[:5])
    rows = [i for i in _items(store, "offer") if i["key"].startswith("offer_missing")]
    assert len(rows) == 2
    one = next(i for i in rows if i["key"] == "offer_missing_import")
    assert one["title"].startswith("5 people from an import") and one["href"] == f"/jobs/{job['id']}"
    assert "and 2 more" in one["detail"]
    assert any(i["key"] == f"offer_missing:{ids[5]}:{job['id']}" for i in rows)  # moved in the app: its own item
    assert "5 people from an import" in local_client(store).get("/").text


# ------------------------------------------------- 4. a held hire keeps its people
def test_a_held_hire_keeps_the_person_at_offer_and_results_stay_after_close(store):
    actions.set_inhouse(store, True)
    jid, tom = _job(store, stage="Offer", hiring_manager="Dana Whitfield")
    iv = store.log(tom, "interview", "Onsite", at=_ago(30), job_id=jid, interviewer="Dana Whitfield")["id"]
    store.update_interview(iv, outcome="yes")
    call(build_server(store), "move_stage", candidate_id=tom, job_id=jid, stage="Hired")
    store.update_job(jid, status="closed")
    rep = reports.report(store, "lumen health", manager="dana whitfield")
    out = reports.report_text(rep)
    assert "Where each person stands:" in out and "Tomas Okafor for Senior Backend Engineer: Offer" in out
    assert "In process now: 1" in out and "Interview results:" in out
    reports.mark_ai_reviewed(store, [m["action_id"] for m in rep["ai_moves"]])
    out = reports.report_text(reports.report(store, "lumen health", manager="dana whitfield"))
    assert "Hired: Tomas Okafor. Req closed: Senior Backend Engineer." in out
    assert "Interview results:" in out and "Tomas Okafor passed Onsite" in out


# ------------------------------------------------- 5. in-house wording and the digest after a close
def test_inhouse_wording_says_hiring_manager(store):
    actions.set_inhouse(store, True)
    jid, _ = _job(store, hiring_manager="Dana Whitfield")
    c = local_client(store)
    page = c.get("/clients/lumen-health").text
    assert "Add a note to the hiring manager this week" in page and "note to the client" not in page
    due = c.get("/clients/due").text
    assert "hiring manager updates are due" in due and "update day on its page" not in due
    call(build_server(store), "update_job", job_id=jid, hiring_manager_email="dana@lumen.com")
    item = next(i for i in _items(store, "ai_change") if i["key"].startswith("aiemail:"))
    assert item["detail"].startswith("Hiring manager updates can be emailed there")
    actions.set_inhouse(store, False)
    assert "Add a note to the client this week" in local_client(store).get("/clients/lumen-health").text


def test_status_digest_for_a_manager_lists_the_req_closed(store):
    actions.set_inhouse(store, True)
    jid, tom = _job(store, stage="Offer", hiring_manager="Dana Whitfield")
    store.move_stage(tom, jid, "Hired")
    store.update_job(jid, status="closed")
    res = call(build_server(store), "status_digest", hiring_manager="Dana")
    out = text(res)
    assert "No open jobs for Dana right now. Closed recently:" in out
    assert "Senior Backend Engineer, Lumen Health, closed" in out and "Hired: Tomas Okafor." in out
    assert data(res)["closed_recently"][0]["hired"] == ["Tomas Okafor"]
    assert actions.status_digest(store, hiring_manager="Nobody") == "No open jobs for Nobody."


# ------------------------------------------------- 6. the small ones
def test_one_day_in_process_is_singular(store):
    jid, cid = _job(store)
    store.log(cid, "interview", "Panel", at=_ago(2), job_id=jid, interviewer="Kim Lee")
    d = decide.finalists(store, jid)
    d["people"][0]["days_in_process"] = 1
    assert "1 day in process" in decide.finalists_text(d) and "1 days" not in decide.finalists_text(d)


def test_lowercase_interviewer_names_read_with_capitals(store):
    assert actions.tidy_names("sam ortiz, Priya Shah") == "Sam Ortiz, Priya Shah"
    assert actions.tidy_names("alex ruiz <alex@lumen.com>") == "Alex Ruiz <alex@lumen.com>"
    assert actions.tidy_names("DJ Patel and kim lee") == "DJ Patel and Kim Lee"
    assert actions.tidy_names("bo@x.com") == "bo@x.com" and actions.tidy_names("Sam de la Cruz") == "Sam de la Cruz"
    jid, cid = _job(store)
    a = store.log(cid, "interview", "Tech", at=_ago(30), job_id=jid, interviewer="sam ortiz")["id"]
    store.log(cid, "interview", "Values", at=_ago(29), job_id=jid, interviewer="Sam Ortiz")
    assert interviews.named(store.get_interview(a)) == ["Sam Ortiz"]
    day = as_local(datetime.now(timezone.utc) - timedelta(hours=30)).date()
    out = interviews.week_text(interviews.week(store, day - timedelta(days=day.weekday())))
    assert "sam ortiz" not in out and "Sam Ortiz 2" in out


def test_needs_more_candidates_counts_openings_left(store):
    job = store.add_job("Backend", company="Acme", openings=2)
    hired = store.add_candidate(name="Hired One")["id"]
    store.add_to_pipeline(hired, job["id"], "Hired")
    thin = next(i for i in _items(store, "thin"))
    assert "Aim for at least 3" in thin["title"]


def test_a_decide_pass_adds_let_them_know_and_undo_closes_it(store):
    jid, tom = _job(store)
    lena = store.add_candidate(name="Lena Brandt")["id"]
    store.add_to_pipeline(lena, jid, "Interviews")
    r = local_client(store).post(f"/jobs/{jid}/decide", data={"offer": str(tom), "pass": str(lena)},
                                 follow_redirects=False)
    assert "Added 1 to-do on Today to let them know." in flash_of(r)
    todo = next(s for s in store.open_suggestions() if s["text"] == "Let Lena Brandt know")
    assert todo["candidate_id"] == lena and todo["job_id"] == jid
    batch = decide.decision_batches(store)[0]
    decide.undo_decision(store, batch["id"])
    assert not [s for s in store.open_suggestions() if s["text"] == "Let Lena Brandt know"]


def test_settings_post_with_only_stuck_days_keeps_the_today_boxes(store):
    store.set_settings(flag_stale="0", flag_thin="1")
    c = local_client(store)
    c.post("/settings", data={"stuck_days": "4"})
    s = store.settings()
    assert s["stuck_days"] == "4" and s.get("flag_thin", "1") == "1" and s["flag_stale"] == "0"
    assert s.get("flag_interviews", "1") == "1"
    c.post("/settings", data={"flag_stale": "1"})
    assert store.settings()["flag_stale"] == "1" and store.settings().get("flag_thin", "1") == "1"
    assert 'name="flags_shown"' in c.get("/settings").text
