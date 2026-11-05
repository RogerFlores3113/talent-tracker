"""Cycle 6, fixer C: interviews and Decide. Prompts and weeks in the recruiter's own zone, weekend days on the
week, each interviewer's own verdict on a joint session, Decide's tally and No offer yet, what's still open, and
the feedback chase (Today item, mailto request, list_feedback_owed)."""
from datetime import date, datetime, timedelta, timezone

import anyio
from mcp.types import CallToolResult

from talent import actions, decide, interviews, recipes, reports, review
from talent.mcp_server import build_server
from talent.store import as_local, set_shown_zone

from conftest import flash_of, local_client

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


def _job(store, name="Tomas Okafor"):
    job = store.add_job("Senior Backend Engineer", company="Lumen Health", stages=INHOUSE)
    cid = store.add_candidate(name=name)["id"]
    store.add_to_pipeline(cid, job["id"], "Interviews")
    return job["id"], cid


def test_prompts_use_the_saved_time_zone_not_the_computers(store):
    try:
        reports.save_user_zone(store, "America/Los_Angeles")
        now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
        for prompt in (recipes.plan_my_day(now), recipes.log_calendar_interviews(now=now)):
            assert "Thursday, November 5, 2026" in prompt and "UTC-08:00" in prompt, prompt
            assert "UTC+00:00" not in prompt
        assert "UTC-08:00" in recipes.render("log_calendar_interviews")
    finally:
        set_shown_zone(None)


def test_week_bounds_follow_the_saved_zone(store):
    try:
        reports.save_user_zone(store, "America/Los_Angeles")
        jid, cid = _job(store)
        store.log(cid, "interview", "Tech screen", at="2026-11-16T05:00:00+00:00", job_id=jid, interviewer="Kim Lee")
        assert interviews.week(store, date(2026, 11, 16))["total"] == 0  # Sun Nov 15, 9 PM in PT
        w = interviews.week(store, date(2026, 11, 9))
        assert w["total"] == 1 and w["days"][-1]["day"] == date(2026, 11, 15) and len(w["days"]) == 6
    finally:
        set_shown_zone(None)


def test_weekend_days_show_only_when_something_is_booked(store):
    reports.save_user_zone(store, "UTC")
    try:
        jid, cid = _job(store)
        assert len(interviews.week(store, date(2026, 11, 16))["days"]) == 5
        store.log(cid, "interview", "Onsite", at="2026-11-21T15:00:00+00:00", job_id=jid, interviewer="Sam Ortiz")
        w = interviews.week(store, date(2026, 11, 16))
        assert [d["day"].weekday() for d in w["days"]] == [0, 1, 2, 3, 4, 5]
        assert w["load"][0]["name"] == "Sam Ortiz" and w["total"] == 1
        page = local_client(store).get("/interviews?week=2026-11-16").text
        assert "Sat Nov 21" in page and "days-6" in page
    finally:
        set_shown_zone(None)


def test_list_interviews_takes_this_and_next(store):
    today = date(2026, 11, 5)
    assert interviews.week_day("this", today) is None
    assert interviews.week_day("next", today) == date(2026, 11, 9)
    assert interviews.week_day("Next week", today) == date(2026, 11, 9)
    assert interviews.week_day("2026-11-18", today) == date(2026, 11, 18)
    res = call(build_server(store), "list_interviews", week="next")
    assert data(res)["start"] == (interviews.week_start() + timedelta(days=7)).isoformat()
    try:
        interviews.week_day("someday")
        raise AssertionError("expected an error")
    except ValueError as exc:
        assert "this or next" in str(exc)


def test_each_interviewer_keeps_their_own_verdict_and_the_session_rolls_up(store):
    jid, cid = _job(store)
    iv = store.log(cid, "interview", "Founder chat", at=_ago(30), job_id=jid, interviewer="Sam Rivera, Priya Nair")["id"]
    try:
        interviews.give_verdict(store, iv, "yes")
        raise AssertionError("expected an error")
    except ValueError as exc:
        assert "Sam Rivera, Priya Nair" in str(exc)
    row = interviews.give_verdict(store, iv, "strong yes", "sam rivera")
    assert row["data"]["verdict"] == "strong_yes" and interviews.owed_by(row) == ["Priya Nair"]
    row = interviews.give_verdict(store, iv, "Mixed", "Priya Nair", "Worried about scope")
    assert row["data"]["verdict"] == "mixed" and row["data"]["outcome"] == "mixed"
    assert row["data"]["feedback"][0]["by"] == "Priya Nair" and not interviews.owed_by(row)
    row = interviews.give_verdict(store, iv, "yes", "Priya Nair", "Slept on it, I'm in")  # a change of mind
    assert row["data"]["verdict"] == "yes" and row["data"]["outcome"] == "passed"
    assert len(row["data"]["verdicts"]) == 3  # the earlier word is kept, the latest counts
    p = decide.finalists(store, jid)["people"][0]
    assert p["tally"] == "Strong yes 1, Yes 1"
    assert [(v["name"], v["label"]) for v in p["interviews"][0]["verdicts"]] == [("Sam Rivera", "Strong yes"),
                                                                                 ("Priya Nair", "Yes")]
    assert interviews.rollup(["strong_yes", "no"]) == "mixed" and interviews.rollup(["no", "strong_no"]) == "no"


def test_a_verdict_waits_for_the_session(store):
    jid, cid = _job(store)
    iv = store.log(cid, "interview", "Onsite", at=_ago(-48), job_id=jid, interviewer="Kim Lee")["id"]
    try:
        interviews.give_verdict(store, iv, "yes")
        raise AssertionError("expected an error")
    except ValueError as exc:
        assert "hasn't happened yet" in str(exc)


def test_decide_tally_inline_verdict_and_open_sessions_warning(store):
    client = local_client(store)
    jid, cid = _job(store)
    lena = store.add_candidate(name="Lena Brandt")["id"]
    store.add_to_pipeline(lena, jid, "Interviews")
    iv = store.log(cid, "interview", "Panel", at=_ago(26), job_id=jid, interviewer="Kim Lee, Alex Ruiz")["id"]
    store.log(lena, "interview", "Tech screen", at=_ago(5), job_id=jid, interviewer="Kim Lee")
    page = client.get(f"/jobs/{jid}/decide").text
    assert "Verdicts: <strong>2 not recorded</strong>" in page
    assert 'list="who-' in page and '<option value="Alex Ruiz">' in page  # Who said it offers each name
    assert "No offer yet, another round" in page and 'data-open="1"' in page and "Still open:" in page
    assert "Lena Brandt has 1 session with no outcome" in page
    assert "—" not in page and "–" not in page
    r = client.post(f"/candidates/{cid}/interviews/{iv}/verdict",
                    data={"interviewer": "Alex Ruiz", "verdict": "strong no", "next": f"/jobs/{jid}/decide"}, follow_redirects=False)
    assert "Strong no" in flash_of(r)
    page = client.get(f"/jobs/{jid}/decide").text
    assert "Verdicts: <strong>Strong no 1, 1 not recorded</strong>" in page and "Owed: Kim Lee" in page
    r = client.post(f"/candidates/{cid}/interviews/{iv}/feedback",
                    data={"interviewer": "Kim Lee", "text": "Great systems depth", "verdict": "yes",
                          "next": f"/jobs/{jid}/decide"}, follow_redirects=False)
    assert flash_of(r) == "Feedback saved."
    assert store.get_interview(iv)["data"]["verdict"] == "mixed"


def test_no_offer_yet_passes_only_who_is_ticked_and_undoes(store):
    client = local_client(store)
    jid, cid = _job(store)
    lena = store.add_candidate(name="Lena Brandt")["id"]
    store.add_to_pipeline(lena, jid, "Interviews")
    store.log(cid, "interview", "Panel", at=_ago(26), job_id=jid)
    r = client.post(f"/jobs/{jid}/decide", data={"offer": "none"}, follow_redirects=False)
    assert "nothing to record" in flash_of(r, "flash_error")
    r = client.post(f"/jobs/{jid}/decide", data={"offer": "none", "pass": [str(lena)]},
                    follow_redirects=False)
    assert flash_of(r).startswith('Recorded: No offer yet, pass on "Lena Brandt".')
    assert store.candidate_pipelines(cid)[0]["stage"] == "Interviews"
    assert store.candidate_pipelines(lena)[0]["stage"] == "Rejected"
    b = decide.decision_batches(store)[0]
    assert "no offer yet, passed on" in b["text"]
    decide.undo_decision(store, b["id"])
    assert store.candidate_pipelines(lena)[0]["stage"] == "Interviews"


def test_days_in_process_count_from_the_first_activity(store):
    job = store.add_job("Founding Engineer", stages=INHOUSE)
    cid = store.add_candidate(name="Marcus Oyelaran")["id"]
    store.log(cid, "interview", "Founder call", at=_ago(24 * 10), job_id=job["id"], interviewer="Sam")
    store.add_to_pipeline(cid, job["id"], "Interviews")
    p = decide.finalists(store, job["id"])["people"][0]
    assert p["days_in_process"] >= 9


def test_today_outcome_card_counts_sessions_still_open_and_offers_strong_no(store):
    jid, cid = _job(store)
    day = (datetime.now(timezone.utc) - timedelta(days=3)).date()  # one day on any clock: times in UTC below
    reports.save_user_zone(store, "UTC")
    a = store.log(cid, "interview", "Coding", at=f"{day}T10:00:00+00:00", job_id=jid, interviewer="Kim Lee")["id"]
    b = store.log(cid, "interview", "Design", at=f"{day}T11:00:00+00:00", job_id=jid, interviewer="Alex Ruiz")["id"]
    c = store.log(cid, "interview", "Values", at=f"{day}T12:00:00+00:00", job_id=jid, interviewer="Sam Ortiz")["id"]
    store.update_interview(a, outcome="yes")
    store.update_interview(b, outcome="yes")
    item = next(i for i in actions.action_items(store)["items"] if i["kind"] == "interview_outcome")
    assert "1 of 3 sessions still needs an outcome" in item["detail"] and item["when"] == store.get_interview(c)["at"]
    page = local_client(store).get("/").text
    assert ">Strong no</button>" in page
    set_shown_zone(None)


def test_feedback_chase_today_item_mailto_and_mcp(store):
    jid, cid = _job(store)
    iv = store.log(cid, "interview", "Onsite: system design", at=_ago(30), job_id=jid,
                   interviewer="Kim Lee, Alex Ruiz <alex@lumen.com>")["id"]
    store.log(cid, "interview", "Fresh one", at=_ago(3), job_id=jid, interviewer="Dana Whitfield")  # under a day
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "feedback_owed"]
    assert len(items) == 1
    owed = items[0]["owed"]
    assert [o["name"] for o in owed] == ["Kim Lee", "Alex Ruiz"]
    assert owed[1]["mailto"].startswith("mailto:alex@lumen.com?subject=") and owed[0]["mailto"].startswith("mailto:?")
    assert "Tomas Okafor" in owed[0]["body"] and "—" not in owed[0]["body"]
    page = local_client(store).get("/").text
    assert "Feedback owed" in page and "Email Alex Ruiz" in page and "Copy request" in page
    res = call(build_server(store), "list_feedback_owed")
    assert "Kim Lee owes feedback on Onsite: system design" in text(res)
    assert data(res)["loops"][0]["owed"][1]["email"] == "alex@lumen.com"
    store.add_interview_feedback(iv, "Strong on tradeoffs", "Kim Lee")
    owed = interviews.feedback_owed(store)[0]["owed"]
    assert [o["name"] for o in owed] == ["Alex Ruiz"]
    interviews.give_verdict(store, iv, "yes", "alex ruiz")
    assert interviews.feedback_owed(store) == []
    assert "Nobody owes" in text(call(build_server(store), "list_feedback_owed"))


def test_interviews_page_shows_needs_outcome_and_who_owes(store):
    jid, cid = _job(store)
    store.log(cid, "interview", "Panel", at=_ago(3), job_id=jid, interviewer="Kim Lee, Alex Ruiz")
    w = interviews.week(store, as_local(datetime.now(timezone.utc) - timedelta(hours=3)).date())
    assert w["owed"] and w["owed"][0]["feedback"] == ["Kim Lee", "Alex Ruiz"] and w["owed"][0]["outcome"]
    assert "feedback owed by Kim Lee, Alex Ruiz" in interviews.week_text(w)


def test_mcp_feedback_with_a_verdict_is_logged_and_waits_in_cautious_mode(store):
    server = build_server(store)
    jid, cid = _job(store)
    iv = store.log(cid, "interview", "Panel", at=_ago(26), job_id=jid, interviewer="Kim Lee, Alex Ruiz")["id"]
    res = call(server, "add_interview_feedback", interview_id=iv, feedback="Loved it", interviewer="Kim Lee",
               verdict="strong yes")
    assert "now reads Strong yes" in text(res)
    acts = [a for a in store.ai_actions() if a["kind"] == "interview_outcome"]
    assert acts and acts[0]["target_id"] == iv
    review.set_cautious(store, True)
    res = call(server, "add_interview_feedback", interview_id=iv, feedback="Not for us", interviewer="Alex Ruiz",
               verdict="no")
    assert "Waiting for the recruiter's review" in text(res) and "verdict: no" in text(res)
    assert store.get_interview(iv)["data"]["verdict"] == "strong_yes"
    review.keep(store, data(res)["pending_review"]["id"])
    assert store.get_interview(iv)["data"]["verdict"] == "mixed"


def test_log_activity_warns_about_a_call_dated_later_than_now(store):
    server = build_server(store)
    cid = store.add_candidate(name="Dana Cole")["id"]
    res = call(server, "log_activity", candidate_id=cid, kind="call", summary="Founder call", when=_ago(-5))
    assert "later than now" in text(res)
    res = call(server, "log_activity", candidate_id=cid, kind="call", summary="Earlier call", when=_ago(5))
    assert "later than now" not in text(res)


def test_founder_stage_set_placeholder_and_unpicked_welcome(store):
    assert actions.stage_set("founder")[:3] == ["Applied", "Founder call", "Technical"]
    client = local_client(store)
    welcome = client.get("/").text
    assert 'aria-pressed' not in welcome and "My own company" in welcome
    form = client.get("/jobs/new").text
    assert "Who makes the hiring call" in form and "Founder-led" in form
