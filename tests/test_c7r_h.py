"""Cycle 7 re-review, fixer H: set_loop_template, plan_loop without a template, verbal/written offers, answer-by
dates, the stale Decide notice and feedback due-by."""
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.types import CallToolResult

from talent import actions, interviews, loops, offers, review
from talent.mcp_server import build_server

from conftest import flash_of, local_client

INHOUSE = list(actions.STAGE_SETS["inhouse"][1])
SESSIONS = [{"label": "System design", "minutes": 60, "interviewers": ["Priya Nair", "Sam Okoye"]},
            {"label": "Behavioral", "minutes": 45, "interviewers": ["Tia Brandt"]}]


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def data(res) -> dict:
    return res.structuredContent if isinstance(res, CallToolResult) else res[1].get("result", res[1])


def _job(store):
    return store.add_job("Senior Data Engineer", company="Lumen Health", stages=INHOUSE)["id"]


def _person(store, jid, name, stage="Interviews"):
    cid = store.add_candidate(name=name)["id"]
    store.add_to_pipeline(cid, jid, stage)
    return cid


# ------------------------------------------------------------ 1. set_loop_template
def test_set_loop_template_saves_logs_and_undoes(store):
    jid = _job(store)
    res = call(build_server(store), "set_loop_template", job_id=jid, sessions=SESSIONS, hours="9-17")
    assert "Saved a loop template of 2 sessions" in text(res)
    t = loops.get_template(store, jid)
    assert [s["name"] for s in t["sessions"]] == ["System design", "Behavioral"]
    assert t["sessions"][0]["interviewers"] == ["Priya Nair", "Sam Okoye"] and t["sessions"][0]["minutes"] == 60
    a = [x for x in store.ai_actions() if x["kind"] == "loop_template"]
    assert len(a) == 1 and "loop template" in a[0]["summary"] and a[0]["undoable"]
    store.undo_ai_action(a[0]["id"])
    assert loops.get_template(store, jid) is None


def test_replacing_a_template_counts_to_the_cap_and_undo_restores_the_old(store):
    jid = _job(store)
    server = build_server(store)
    call(server, "set_loop_template", job_id=jid, sessions=SESSIONS)
    call(server, "set_loop_template", job_id=jid, sessions=[{"label": "Panel", "minutes": 30}])
    assert [s["name"] for s in loops.get_template(store, jid)["sessions"]] == ["Panel"]
    newest = [x for x in store.ai_actions() if x["kind"] == "loop_template"][0]
    store.undo_ai_action(newest["id"])
    assert [s["name"] for s in loops.get_template(store, jid)["sessions"]] == ["System design", "Behavioral"]


def test_update_job_accepts_loop_template_as_an_alias(store):
    jid = _job(store)
    res = call(build_server(store), "update_job", job_id=jid, loop_template=SESSIONS)
    assert "Saved a loop template" in text(res)
    assert len(loops.get_template(store, jid)["sessions"]) == 2
    res = call(build_server(store), "update_job", job_id=jid, loop_template=SESSIONS, location="Chicago")
    assert "Chicago" in text(res) and store.get_job(jid)["location"] == "Chicago"


def test_cautious_mode_holds_the_template(store):
    jid = _job(store)
    review.set_cautious(store, True)
    res = call(build_server(store), "set_loop_template", job_id=jid, sessions=SESSIONS)
    assert "Waiting for the recruiter's review" in text(res)
    assert loops.get_template(store, jid) is None
    row = review.pending(store)[0]
    assert row["kind"] == "loop_template"
    review.keep(store, row["id"])
    assert loops.get_template(store, jid) is not None


def test_a_bad_template_changes_nothing(store):
    jid = _job(store)
    with pytest.raises(Exception) as exc:
        call(build_server(store), "set_loop_template", job_id=jid, sessions=[{"label": "", "minutes": 5}])
    assert "No change made" in str(exc.value) or "session" in str(exc.value)
    assert loops.get_template(store, jid) is None


# ------------------------------------------------------------ 2. plan_loop
def test_plan_loop_without_a_template_says_so_before_asking_for_a_candidate(store):
    jid = _job(store)
    res = call(build_server(store), "plan_loop", job_id=jid)
    assert "no loop template" in text(res) and "set_loop_template" in text(res)
    assert data(res)["has_template"] is False


def test_plan_loop_with_a_template_still_needs_a_candidate(store):
    jid = _job(store)
    loops.save_template(store, jid, "System design | 60 | Priya Nair")
    with pytest.raises(Exception) as exc:
        call(build_server(store), "plan_loop", job_id=jid)
    assert "candidate_id" in str(exc.value)


# ------------------------------------------------------------ 3. verbal and written offers
@pytest.mark.parametrize("word", ["verbal", "written", "extended", "sent"])
def test_record_offer_takes_verbal_and_written_as_out(store, word):
    jid = _job(store)
    cid = _person(store, jid, "Dana Cho", "Offer")
    res = call(build_server(store), "record_offer", candidate_id=cid, job_id=jid, salary="180k", status=word)
    assert offers.get(store, cid, jid)["status"] == "out"
    assert data(res)["offer"]["offer_form"] == word and f"{word} offer" in text(res)


def test_record_offer_terms_is_an_alias_for_notes(store):
    jid = _job(store)
    cid = _person(store, jid, "Dana Cho", "Offer")
    call(build_server(store), "record_offer", candidate_id=cid, job_id=jid, terms="Wants a Jan start")
    assert offers.get(store, cid, jid)["notes"] == "Wants a Jan start"


def test_the_schema_lists_the_accepted_status_words(store):
    tools = anyio.run(build_server(store).list_tools)
    schema = next(t for t in tools if t.name == "record_offer").inputSchema
    enum = schema["properties"]["status"]["anyOf"][0]["enum"]
    assert {"drafted", "out", "verbal", "written"} <= set(enum)


# ------------------------------------------------------------ 4. answer-by
def test_list_offers_shows_the_answer_by_date(store):
    jid = _job(store)
    cid = _person(store, jid, "Dana Cho", "Offer")
    server = build_server(store)
    call(server, "record_offer", candidate_id=cid, job_id=jid, salary="180k", answer_by="2030-03-08")
    res = call(server, "list_offers")
    assert "answer by Fri Mar 8" in text(res) or "answer by" in text(res)
    assert data(res)["offers"][0]["answer_by"] == "2030-03-08" and data(res)["offers"][0]["answer_by_text"]
    assert "answer by" in offers.status_line(offers.get(store, cid, jid))


# ------------------------------------------------------------ 5. stale Decide
def test_a_stale_decide_resubmit_shows_a_fresh_form_with_a_notice(store):
    jid = _job(store)
    dana, omar = _person(store, jid, "Dana Cho"), _person(store, jid, "Omar Ali")
    client = local_client(store)
    form = {"offer": dana, "pass": [omar], "confirmed": "1"}
    client.post(f"/jobs/{jid}/decide", data=form, follow_redirects=False)
    r = client.post(f"/jobs/{jid}/decide", data=form, follow_redirects=False)
    assert r.headers["location"].startswith(f"/jobs/{jid}/decide")
    assert flash_of(r) == "Things changed since you opened this. Here's the latest."
    page = client.get(f"/jobs/{jid}/decide")
    assert page.status_code == 200


# ------------------------------------------------------------ 6. feedback due-by
def test_owed_feedback_has_a_due_time_and_an_overdue_badge(store):
    jid = _job(store)
    cid = _person(store, jid, "Dana Cho")
    at = (datetime.now(timezone.utc) - timedelta(hours=80)).replace(microsecond=0)
    iv = store.log(cid, "interview", "Panel", at=at.isoformat(), job_id=jid, interviewer="Priya Nair")["id"]
    w = interviews.week(store, at.date())
    sess = [x for lp in w["loops"] for x in lp["sessions"] if x["interview_id"] == iv][0]
    assert sess["owed"] and sess["feedback_overdue"] is True and sess["feedback_due"]
    html = local_client(store).get(f"/interviews?week={at.date().isoformat()}").text
    assert "Feedback overdue" in html
    store.add_interview_feedback(iv, "Solid", "Priya Nair")
    w = interviews.week(store, at.date())
    sess = [x for lp in w["loops"] for x in lp["sessions"] if x["interview_id"] == iv][0]
    assert not sess["owed"] and "feedback_due" not in sess
