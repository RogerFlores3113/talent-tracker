"""Cycle 8, fixer M: book_loop says what it did not place and takes plan_loop's slot shape, mark_chased and the
snoozed nudge, greetings from a contact name, and Keep into Contacted agreeing with Outreach."""
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from talent import actions, chase, loops, reports, review
from talent.mcp_server import build_server
from talent.store import set_shown_zone

from conftest import local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def data(res) -> dict:
    return res.structuredContent if isinstance(res, CallToolResult) else res[1].get("result", res[1])


def _at(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _age(store, cid, jid, days):
    with store._lock, store._conn:
        store._conn.execute("UPDATE applications SET stage_changed_at = ? WHERE candidate_id = ? AND job_id = ?",
                            (_at(-days), cid, jid))


@pytest.fixture(autouse=True)
def _zone(store):
    reports.save_user_zone(store, "America/Chicago")
    yield
    set_shown_zone(None)


# ------------------------------------------------------------ book_loop
def _loop_job(store):
    jid = store.add_job("Data Engineer", company="Lumen Health", stages=list(actions.STAGE_SETS["inhouse"][1]))["id"]
    loops.save_template(store, jid, "System design | 60 | Priya Nair\nCoding | 45 | Sam Okoye", "9-17")
    cid = store.add_candidate(name="Dana Cho")["id"]
    store.add_to_pipeline(cid, jid, "Interviews")
    return jid, cid


def test_book_loop_lists_what_it_did_not_place(store):
    jid, cid = _loop_job(store)
    server = build_server(store)
    res = call(server, "book_loop", job_id=jid, candidate_id=cid, start="2030-01-07", unavailable=["Sam Okoye"])
    t = text(res)
    assert "Not booked: Coding (no free interviewer)" in t and "Next step" in t
    d = data(res)
    assert d["not_booked"] == [{"session": "Coding", "reason": "no free interviewer"}] and d["next_step"]
    assert len(d["booked"]) == 1
    assert "—" not in t and "–" not in t


def test_the_plan_warning_says_who_is_out(store):
    jid, cid = _loop_job(store)
    p = loops.plan(store, jid, cid, "2030-01-07", ["Sam Okoye"])
    coding = next(s for s in p["sessions"] if s["name"] == "Coding")
    assert coding["warnings"][0].startswith("Sam Okoye is out and no one else is listed for Coding")
    assert "and anyone" not in " ".join(coding["warnings"])


def test_book_loop_takes_the_slots_plan_loop_returns(store):
    jid, cid = _loop_job(store)
    server = build_server(store)
    plan = data(call(server, "plan_loop", job_id=jid, candidate_id=cid, start="2030-01-07"))
    slots = [{**s, "interviewers": [s["interviewer"]], "minutes": 60, "extra": {"x": 1}} for s in plan["sessions"]]
    slots[0]["interviewer"] = None  # a list under interviewers is enough
    res = call(server, "book_loop", job_id=jid, candidate_id=cid, slots=slots)
    assert len(data(res)["booked"]) == 2 and data(res)["booked"][0]["interviewer"] == "Priya Nair"


def test_book_loop_type_errors_are_plain(store):
    jid, cid = _loop_job(store)
    server = build_server(store)
    with pytest.raises(ToolError) as e:
        call(server, "book_loop", job_id=jid, candidate_id=cid, slots=["System design at 9"])
    assert "It accepts: session, at, interviewer" in str(e.value) and "pydantic" not in str(e.value).lower()
    with pytest.raises(ToolError) as e:
        call(server, "book_loop", job_id=jid, candidate_id=cid, slots=[{"session": "Coding", "at": 5}])
    assert "It accepts:" in str(e.value) and "Input should be" not in str(e.value)
    with pytest.raises(ToolError) as e:
        call(server, "book_loop", job_id=jid, candidate_id=cid, start=["x"])  # a type error anywhere
    assert "It accepts:" in str(e.value) and "Input should be" not in str(e.value) and "validation error" not in str(e.value)


# ------------------------------------------------------------ mark_chased
def _chasable(store, contacts=None, manager=None):
    job = store.add_job("Analyst", company="Pinecrest Capital", stages=actions.stage_set("agency"),
                        **({"hiring_manager": manager} if manager else {}))
    if contacts:
        reports.save_client(store, "Pinecrest Capital", contacts=contacts)
    cid = store.add_candidate(name="Lena Hoffmann")["id"]
    store.add_to_pipeline(cid, job["id"], "Submitted")
    _age(store, cid, job["id"], 8)
    return job, cid


def test_mark_chased_snoozes_the_nudge_in_client_chase_and_the_desk(store):
    _chasable(store, "Dana Ortiz <dana@pinecrest.com>")
    server = build_server(store)
    assert "Hi Dana," in text(call(server, "client_chase", client="pinecrest"))
    res = call(server, "mark_chased", client="pinecrest")
    assert "Marked the nudges to" in text(res) and "Nothing was sent" in text(res)
    today = datetime.now(timezone.utc)
    want = reports.in_zone(today.isoformat(), "America/Chicago", "%b %-d")
    assert f"Chased {want}, next " in text(res) or "Chased " in text(res)
    t = text(call(server, "client_chase", client="pinecrest"))
    assert "Chased " in t and ", next " in t and "Hi Dana" not in t
    d = data(call(server, "client_chase", client="pinecrest"))
    assert d["nudges"][0]["text"] is None and d["nudges"][0]["snoozed_until"]
    desk = call(server, "client_desk", client="pinecrest")
    assert "Hi Dana" not in text(desk) and ", next " in text(desk)
    page = local_client(store).get("/clients/pinecrest-capital/desk").text
    assert "Hi Dana" not in page and ", next " in page and "Mark as chased" not in page
    assert "—" not in page


def test_mark_chased_with_snooze_days_is_logged_and_undoable(store):
    _chasable(store)
    server = build_server(store)
    res = call(server, "mark_chased", company="pinecrest", snooze_days=14)
    aid = data(res)["action_id"]
    assert (reports.in_zone(data(res)["next_nudge_on"], None, "%b %-d")) in text(res)
    row = next(a for a in store.ai_actions() if a["id"] == aid)
    assert "snoozed 14 days" in row["summary"] and row["undoable"]
    store.undo_ai_action(aid)
    assert chase.chased_at(store, "pinecrest capital", "") is None
    assert chase.chase_list(store)[0]["next_on"] is None
    with pytest.raises(Exception, match="1 to 365"):
        call(server, "mark_chased", client="pinecrest", snooze_days=900)


def test_mark_chased_waits_in_cautious_mode_and_checks_the_client(store):
    _chasable(store)
    review.set_cautious(store, True)
    server = build_server(store)
    res = call(server, "mark_chased", client="pinecrest")
    assert "Waiting for the recruiter's review" in text(res)
    assert chase.chase_list(store)[0]["next_on"] is None
    with pytest.raises(Exception, match="No client matches"):
        call(server, "mark_chased", client="nobody")
    review.keep(store, review.pending(store)[0]["id"])
    assert chase.chase_list(store)[0]["next_on"] is not None


# ------------------------------------------------------------ greeting
def test_nudge_and_submittal_greet_the_first_contact_name(store):
    job, cid = _chasable(store, "Priya Rao <priya@acmepay.com>\nOmar Ali <omar@acmepay.com>")
    g = chase.chase_list(store)[0]
    assert g["text"].startswith("Hi Priya,")
    assert chase.writeup(store, cid, job["id"])["body"].startswith("Hi Priya,")


def test_a_named_hiring_manager_still_wins_and_a_bare_email_says_there(store):
    _chasable(store, "Priya Rao <priya@acmepay.com>", manager="Dana Reyes")
    assert chase.chase_list(store)[0]["text"].startswith("Hi Dana,")
    assert reports.contact_name("priya@acmepay.com") == "" and reports.contact_name("") == ""
    assert reports.contact_name("Priya Rao, VP People <p@x.com>") == "Priya Rao"


# ------------------------------------------------------------ Keep into Contacted
def test_keep_into_contacted_agrees_with_outreach(store):
    from talent import outreach
    job = store.add_job("ML Engineer", company="Northwind")
    cid = store.add_candidate(name="Maya Okafor")["id"]
    store.add_to_pipeline(cid, job["id"], "Contacted")
    assert store.get_candidate(cid)["last_contacted_at"] is None  # not messaged yet, on both pages
    assert outreach.table(store, job["id"])["people"] == []
    store.log(cid, "email", "Hello", job_id=job["id"])
    assert store.get_candidate(cid)["last_contacted_at"]
    assert [p["candidate_id"] for p in outreach.table(store, job["id"])["people"]] == [cid]
