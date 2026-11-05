"""Cycle 7, fixer B: loose interviewer names, one nudge per interviewer, Decide warns on missing feedback, and the
loop planner (template, slots, swaps, invites, booking)."""
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.types import CallToolResult

from talent import actions, decide, interviews, loops, reports
from talent.mcp_server import build_server
from talent.store import set_shown_zone

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


def _job(store):
    return store.add_job("Senior Data Engineer", company="Lumen Health", stages=INHOUSE)["id"]


def _person(store, jid, name, stage="Interviews"):
    cid = store.add_candidate(name=name)["id"]
    store.add_to_pipeline(cid, jid, stage)
    return cid


@pytest.fixture
def chicago(store):
    reports.save_user_zone(store, "America/Chicago")
    yield
    set_shown_zone(None)


# ------------------------------------------------------------ 1. loose names
def test_first_names_match_the_named_interviewer(store):
    jid = _job(store)
    cid = _person(store, jid, "Dana Cho")
    iv = store.log(cid, "interview", "Panel", at=_ago(30), job_id=jid, interviewer="Priya Nair, Tia Brandt")["id"]
    store.add_interview_feedback(iv, "Solid", "priya")
    assert store.get_interview(iv)["data"]["feedback"][0]["by"] == "Priya Nair"
    assert interviews.owed_by(store.get_interview(iv)) == ["Tia Brandt"]
    interviews.give_verdict(store, iv, "yes", "Tia B.")
    got = store.get_interview(iv)
    assert got["data"]["verdicts"][0]["by"] == "Tia Brandt" and not interviews.owed_by(got)
    # Priya gave feedback but no verdict of her own, so one verdict is still not recorded
    assert interviews.tally([{"data": got["data"], "status": "passed", "outcome": "Passed"}])["missing"] == 1


def test_old_ghost_names_still_count_and_ambiguous_names_say_so(store):
    jid = _job(store)
    cid = _person(store, jid, "Dana Cho")
    iv = store.log(cid, "interview", "Panel", at=_ago(30), job_id=jid, interviewer="Priya Nair, Tia Brandt")["id"]
    row = store.get_interview(iv)
    row["data"]["feedback"] = [{"by": "Priya", "text": "Old", "at": _ago(5), "source": "App"}]  # saved before matching
    assert interviews.owed_by(row) == ["Tia Brandt"]
    two = store.log(cid, "interview", "Pairing", at=_ago(29), job_id=jid, interviewer="Priya Nair, Priya Shah")["id"]
    with pytest.raises(ValueError, match="could be Priya Nair or Priya Shah"):
        store.add_interview_feedback(two, "Good", "Priya")
    with pytest.raises(ValueError, match="Say which one"):
        interviews.give_verdict(store, two, "yes", "priya")
    res = call(build_server(store), "add_interview_feedback", interview_id=iv, feedback="Fine", interviewer="tia b.")
    assert "Tia Brandt" in text(res) and "Still owed on that session: Priya Nair" in text(res)


# ------------------------------------------------------------ 2. one nudge per interviewer
def test_one_nudge_per_interviewer_across_candidates(store):
    jid = _job(store)
    names = ["Dana Cho", "Omar Ali", "Lena Wu"]
    for n, h in zip(names, (32, 31, 30)):
        cid = _person(store, jid, n)
        store.log(cid, "interview", "System design", at=_ago(h), job_id=jid, interviewer="Priya Shah <priya@lumen.com>")
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "feedback_owed"]
    assert len(items) == 1 and items[0]["who"] == "Priya Shah" and len(items[0]["owed"]) == 3
    assert items[0]["subject"] == "Feedback on Dana Cho, Omar Ali and Lena Wu"
    assert items[0]["mailto"].startswith("mailto:priya@lumen.com?subject=")
    assert "Dana Cho: System design" in items[0]["body"] and "—" not in items[0]["body"]
    page = local_client(store).get("/").text
    assert "Priya Shah owes feedback on 3 interviews" in page and page.count("Copy request") == 1
    res = call(build_server(store), "list_feedback_owed")
    assert "Priya Shah owes 3: Dana Cho, Omar Ali, Lena Wu" in text(res)
    assert data(res)["people"][0]["owes"] == 3 and len(data(res)["loops"]) == 3
    w = interviews.week(store, actions.as_local(datetime.now(timezone.utc) - timedelta(hours=30)).date())
    assert w["owed_people"] and w["owed_people"][0]["name"] == "Priya Shah"
    html = local_client(store).get("/interviews?week=" + w["start"].isoformat()).text
    assert "Feedback owed, by interviewer" in html


def test_a_first_name_and_a_full_name_are_one_person_in_the_owed_list(store):
    jid = _job(store)
    a, b = _person(store, jid, "Dana Cho"), _person(store, jid, "Omar Ali")
    store.log(a, "interview", "Pairing", at=_ago(30), job_id=jid, interviewer="Priya Shah")
    store.log(b, "interview", "Pairing", at=_ago(31), job_id=jid, interviewer="priya")
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "feedback_owed"]
    assert len(items) == 1 and len(items[0]["owed"]) == 2


# ------------------------------------------------------------ 3. Decide warns
def _decide_setup(store):
    jid = _job(store)
    dana, omar = _person(store, jid, "Dana Cho"), _person(store, jid, "Omar Ali")
    d = store.log(dana, "interview", "Panel", at=_ago(40), job_id=jid, interviewer="Sam Okoye, Tia Brandt")["id"]
    store.log(omar, "interview", "Panel", at=_ago(41), job_id=jid, interviewer="Sam Okoye")
    interviews.give_verdict(store, d, "yes", "Sam")
    return jid, dana, omar, d


def test_decide_warns_before_offer_or_reject_and_can_add_nudge_todos(store):
    jid, dana, omar, d = _decide_setup(store)
    client = local_client(store)
    r = client.post(f"/jobs/{jid}/decide", data={"offer": dana, "pass": [omar]}, follow_redirects=False)
    page = client.get(f"/jobs/{jid}/decide").text
    assert r.status_code == 200 and "Before you record this" in r.text
    assert "Dana Cho: Tia Brandt still owes feedback" in r.text and "Omar Ali: Sam Okoye still owes feedback" in r.text
    assert "Before you record this" not in page  # nothing was recorded, and a plain page has no warning
    assert {p["stage"] for p in store.pipeline(jid)} == {"Interviews"}
    r = client.post(f"/jobs/{jid}/decide", data={"offer": dana, "pass": [omar], "confirmed": "1", "nudge": "1",
                                                  "reason": "Not a fit"}, follow_redirects=False)
    assert "Added 2 to-dos to ask for the missing feedback" in flash_of(r)
    todos = [s["text"] for s in store.open_suggestions(jid)]
    assert "Ask Tia Brandt for feedback on Dana Cho" in todos and "Ask Sam Okoye for feedback on Omar Ali" in todos
    batch = decide.decision_batches(store)[0]
    client.post(f"/changes/decision/{batch['id']}/undo", follow_redirects=False)
    assert not [t for t in store.open_suggestions(jid) if t["text"].startswith("Ask ")]


def test_nothing_missing_records_straight_away(store):
    jid = _job(store)
    dana = _person(store, jid, "Dana Cho")
    r = local_client(store).post(f"/jobs/{jid}/decide", data={"offer": dana}, follow_redirects=False)
    assert r.status_code == 303 and "Recorded: Offer to" in flash_of(r)


def test_status_update_says_verdict_missing_not_passed(store):
    jid, dana, omar, d = _decide_setup(store)
    rows = [r for r in actions.interview_results(store, jid) if r["candidate_id"] == dana]
    assert rows and rows[0]["missing"] == 1
    assert "1 verdict missing" in rows[0]["line"] and "passed" not in rows[0]["line"]
    interviews.give_verdict(store, d, "yes", "Tia")
    row = [r for r in actions.interview_results(store, jid) if r["candidate_id"] == dana][0]
    assert "passed" in row["line"] and "missing" not in row["line"]


def test_a_stale_decide_resubmit_returns_to_a_fresh_decide_form(store):
    jid = _job(store)
    dana, omar = _person(store, jid, "Dana Cho"), _person(store, jid, "Omar Ali")
    client = local_client(store)
    form = {"offer": dana, "pass": [omar], "confirmed": "1"}
    client.post(f"/jobs/{jid}/decide", data=form, follow_redirects=False)
    r = client.post(f"/jobs/{jid}/decide", data=form, headers={"referer": f"http://127.0.0.1:8765/jobs/{jid}"},
                    follow_redirects=False)
    assert r.headers["location"].startswith(f"/jobs/{jid}/decide")
    assert "isn't in play" in flash_of(r, "flash_error")


# ------------------------------------------------------------ S: session length
def test_thirty_minute_sessions_back_to_back_are_not_a_clash(store):
    jid = _job(store)
    cid = _person(store, jid, "Dana Cho")
    day = datetime(2030, 1, 8, 15, 0, tzinfo=timezone.utc)  # a Tuesday
    store.log(cid, "interview", "Intro (30 min)", at=day.isoformat(), job_id=jid, interviewer="Sam Okoye")
    store.log(cid, "interview", "Pairing (30 min)", at=(day + timedelta(minutes=30)).isoformat(), job_id=jid,
              interviewer="Sam Okoye")
    w = interviews.week(store, actions.as_local(day).date())
    assert not [c for c in w["clashes"] if c["kind"] == "interviewer"]
    store.log(cid, "interview", "Extra", at=(day + timedelta(minutes=45)).isoformat(), job_id=jid, interviewer="Sam Okoye")
    w = interviews.week(store, actions.as_local(day).date())
    assert [c for c in w["clashes"] if c["kind"] == "interviewer"]


# ------------------------------------------------------------ 4. the loop planner
TEMPLATE = "System design | 60 | Priya Nair, Sam Okoye\nPairing | 45 | Omar Ali\nBehavioral | 30 | Tia Brandt"
MONDAY = "2030-01-07"


def test_template_parses_saves_and_rejects_bad_lines(store):
    jid = _job(store)
    s = loops.parse_template(TEMPLATE + "\nCoffee chat")
    assert [x["name"] for x in s] == ["System design", "Pairing", "Behavioral", "Coffee chat"]
    assert s[0] == {"name": "System design", "minutes": 60, "interviewers": ["Priya Nair", "Sam Okoye"]}
    assert s[3]["minutes"] == 45 and s[3]["interviewers"] == []
    with pytest.raises(ValueError, match="15 to 240"):
        loops.parse_template("Quick | 5 | Sam")
    with pytest.raises(ValueError, match="listed twice"):
        loops.parse_template("A | 30\na | 30")
    with pytest.raises(ValueError, match="Working hours"):
        loops.parse_hours("late")
    loops.save_template(store, jid, TEMPLATE, "9-17")
    assert loops.get_template(store, jid)["hours"] == [9, 17]
    assert loops.template_text(loops.get_template(store, jid)["sessions"]).splitlines()[1] == "Pairing | 45 | Omar Ali"


def test_plan_avoids_booked_interviewers_swaps_and_warns(store, chicago):
    jid = _job(store)
    loops.save_template(store, jid, TEMPLATE, "9-17")
    dana, other = _person(store, jid, "Dana Cho"), _person(store, jid, "Kim Lee")
    # Priya is busy 9:00 to 10:00 Chicago time on Monday, so Sam takes system design.
    nine = datetime(2030, 1, 7, 15, 0, tzinfo=timezone.utc)  # 9:00 AM CST
    store.log(other, "interview", "System design (60 min)", at=nine.isoformat(), job_id=jid, interviewer="Priya Nair")
    p = loops.plan(store, jid, dana, MONDAY, now=datetime(2029, 12, 1, tzinfo=timezone.utc))
    sd, pair, beh = p["sessions"]
    assert sd["interviewer"] == "Sam Okoye" and sd["at_local"].startswith("Mon Jan 7, 9:00 AM")
    assert any("Priya Nair is booked then" in w for w in sd["warnings"])
    assert pair["interviewer"] == "Omar Ali" and beh["interviewer"] == "Tia Brandt"
    assert pair["at_local"].startswith("Mon Jan 7, 10:30 AM")  # a break after the 60 minute session, then the next half hour
    assert p["invites"] and all("—" not in i["body"] and "–" not in i["body"] for i in p["invites"])
    assert "Hi Dana," in p["candidate_invite"]["body"] and "Behavioral" in p["candidate_invite"]["body"]
    # Sam is out too: no one is left for system design that Monday morning with Priya busy, so it lands later or flags.
    p2 = loops.plan(store, jid, dana, MONDAY, unavailable=["sam"], now=datetime(2029, 12, 1, tzinfo=timezone.utc))
    sd2 = p2["sessions"][0]
    assert sd2["interviewer"] == "Priya Nair" and sd2["at_local"].startswith("Mon Jan 7, 10:00 AM")
    p3 = loops.plan(store, jid, dana, MONDAY, unavailable=["Priya", "Sam"], now=datetime(2029, 12, 1, tzinfo=timezone.utc))
    assert not p3["sessions"][0]["placed"] and "Not placed: System design" in " ".join(p3["warnings"])


def test_plan_skips_weekends_and_sessions_already_booked(store, chicago):
    jid = _job(store)
    loops.save_template(store, jid, "Pairing | 45 | Omar Ali", "9-17")
    dana = _person(store, jid, "Dana Cho")
    p = loops.plan(store, jid, dana, "2030-01-05", now=datetime(2029, 12, 1, tzinfo=timezone.utc))  # a Saturday
    assert p["sessions"][0]["at_local"].startswith("Mon Jan 7")
    loops.book(store, dana, jid, loops.slots_from(p), by="app")
    again = loops.plan(store, jid, dana, "2030-01-05", now=datetime(2029, 12, 1, tzinfo=timezone.utc))
    assert again["sessions"][0]["already"] and "booked twice" in again["sessions"][0]["warnings"][0]
    with pytest.raises(ValueError, match="no loop template"):
        loops.plan(store, _job(store), dana)


def test_loop_page_template_form_plan_and_book(store, chicago):
    jid = _job(store)
    dana = _person(store, jid, "Dana Cho")
    client = local_client(store)
    r = client.post(f"/jobs/{jid}/loop/template", data={"sessions": TEMPLATE, "hours": "9-17"}, follow_redirects=False)
    assert "Saved the loop template: 3 sessions" in flash_of(r)
    r = client.post(f"/jobs/{jid}/loop/template", data={"sessions": "Quick | 5", "hours": "9-17"}, follow_redirects=False)
    assert "15 to 240" in flash_of(r, "flash_error")
    html = client.get(f"/jobs/{jid}/loop?candidate={dana}&start={MONDAY}").text
    assert "Proposed loop for Dana Cho" in html and "Log these interviews" in html and "Copy invite" in html
    assert "—" not in html and "–" not in html and 'href="/jobs/%d/loop"' % jid in html
    p = loops.plan(store, jid, dana, MONDAY)
    form = {"candidate_id": dana, "s_name": [s["name"] for s in p["sessions"]], "s_at": [s["at"] for s in p["sessions"]],
            "s_min": [s["minutes"] for s in p["sessions"]], "s_who": [s["interviewer"] for s in p["sessions"]]}
    r = client.post(f"/jobs/{jid}/loop/book", data=form, follow_redirects=False)
    assert "Logged 3 interviews" in flash_of(r)
    ivs = [t for t in store.timeline(dana) if t["kind"] == "interview"]
    assert len(ivs) == 3 and any(t["body"] == "System design (60 min)" for t in ivs)


def test_mcp_plan_loop_and_book_loop_log_ai_writes_that_undo(store, chicago):
    jid = _job(store)
    loops.save_template(store, jid, TEMPLATE, "9-17")
    dana = _person(store, jid, "Dana Cho")
    server = build_server(store)
    res = call(server, "plan_loop", job_id=jid, candidate_id=dana, start=MONDAY)
    d = data(res)
    assert len(d["sessions"]) == 3 and d["interviewer_invites"] and d["candidate_invite"]["subject"]
    assert "Nothing is booked" in text(res) and not [t for t in store.timeline(dana) if t["kind"] == "interview"]
    res = call(server, "book_loop", job_id=jid, candidate_id=dana, start=MONDAY)
    assert "Logged 3 interviews" in text(res) and len(data(res)["booked"]) == 3
    ivs = [t for t in store.timeline(dana) if t["kind"] == "interview"]
    assert len(ivs) == 3 and all(t["source"] == "AI app" for t in ivs)
    changes = local_client(store).get("/changes").text
    assert "Dana Cho" in changes and "System design" in changes
    with pytest.raises(Exception, match="Nothing booked"):
        call(server, "book_loop", job_id=jid, candidate_id=dana, start=MONDAY)  # all three are already booked


def test_book_loop_with_slots_warns_about_a_busy_interviewer_and_cautious_mode_holds(store, chicago):
    jid = _job(store)
    loops.save_template(store, jid, TEMPLATE, "9-17")
    dana, kim = _person(store, jid, "Dana Cho"), _person(store, jid, "Kim Lee")
    store.log(kim, "interview", "Pairing (45 min)", at="2030-01-07T16:00:00+00:00", job_id=jid, interviewer="Omar Ali")
    server = build_server(store)
    res = call(server, "book_loop", job_id=jid, candidate_id=dana,
               slots=[{"session": "Pairing", "at": "2030-01-07T16:15:00+00:00", "interviewer": "Omar Ali"}])
    assert "Omar Ali is booked then" in text(res)
    with pytest.raises(Exception, match="session must be one of"):
        call(server, "book_loop", job_id=jid, candidate_id=dana, slots=[{"session": "Lunch", "at": "2030-01-08T16:00:00+00:00"}])


def test_book_loop_waits_for_review_in_cautious_mode(store, chicago):
    from talent import review
    jid = _job(store)
    loops.save_template(store, jid, "Pairing | 45 | Omar Ali", "9-17")
    dana = _person(store, jid, "Dana Cho")
    review.set_cautious(store, True)
    res = call(build_server(store), "book_loop", job_id=jid, candidate_id=dana, start=MONDAY)
    assert "Waiting for the recruiter's review" in text(res)
    assert not [t for t in store.timeline(dana) if t["kind"] == "interview"]
