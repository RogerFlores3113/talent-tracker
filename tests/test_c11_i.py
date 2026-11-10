"""c11 I: the Interviews, Feedback owed and Duplicates views, their richer structured data, and the wrong-id hint."""
import re
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.types import CallToolResult

from talent import actions, interviews
from talent.mcp_server import build_server
from talent.mcp_views import RESOURCE_META, URIS, tool_meta, view_html

INHOUSE = list(actions.STAGE_SETS["inhouse"][1])
VIEWS = {"interviews": "list_interviews", "owed": "list_feedback_owed", "duplicates": "find_duplicates"}


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


def _person(store, jid, name):
    cid = store.add_candidate(name=name)["id"]
    store.add_to_pipeline(cid, jid, "Interviews")
    return cid


def test_each_tool_has_its_view_and_the_views_are_clean():
    for name, uri in ((n, URIS[n]) for n in VIEWS):
        html = view_html(name)
        assert not re.findall(r"""(?:src|href)\s*=\s*['"]?https?://""", html), name
        assert "innerHTML" not in html and "outerHTML" not in html and "document.write" not in html, name
        assert "fetch(" not in html and "XMLHttpRequest" not in html, name
        assert "—" not in html and "–" not in html, name
        assert "function render(" in html and uri.startswith("ui://talent-search/")
        assert tool_meta(name)["ui"]["resourceUri"] == uri
    assert RESOURCE_META["ui"]["prefersBorder"]


def test_tools_carry_the_resource_uri_and_resources_are_registered(store):
    server = build_server(store)
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    for name, tool in VIEWS.items():
        assert tools[tool].meta["ui"]["resourceUri"] == URIS[name]
    uris = {str(r.uri) for r in anyio.run(server.list_resources)}
    assert {URIS[n] for n in VIEWS} <= uris


def test_list_interviews_carries_what_the_agenda_needs(store):
    jid = _job(store)
    a = _person(store, jid, "Dana Cho")
    store.log(a, "interview", "Panel (30 min)", at=_ago(-20), job_id=jid, interviewer="Priya Shah, Sam Okoye")
    store.log(a, "interview", "Culture chat", at=_ago(-20.1), job_id=jid, interviewer="Priya Shah")
    res = call(build_server(store), "list_interviews", week=(datetime.now(timezone.utc) + timedelta(hours=20)).date().isoformat())
    d = data(res)
    lp = d["loops"][0]
    assert lp["candidate_url"].endswith(f"/candidates/{a}") and lp["job_url"].endswith(f"/jobs/{jid}")
    s = lp["sessions"][0]
    assert s["at"] and s["end"] and s["interviewers"] and s["app_url"].startswith("http")
    assert s["at"] < s["end"] and "now" in d and "tz" in d
    assert d["clashes"] and d["load"][0]["interviewer"] == "Priya Shah"
    assert "Interviews" in text(res)  # the text reply stays complete on its own


def test_feedback_owed_lists_each_session_with_days_overdue(store):
    jid = _job(store)
    a, b = _person(store, jid, "Dana Cho"), _person(store, jid, "Omar Ali")
    store.log(a, "interview", "Pairing", at=_ago(80), job_id=jid, interviewer="Priya Shah")
    store.log(b, "interview", "Pairing", at=_ago(40), job_id=jid, interviewer="Priya Shah")
    res = call(build_server(store), "list_feedback_owed")
    p = data(res)["people"][0]
    assert p["name"] == "Priya Shah" and len(p["items"]) == 2
    by = {i["candidate"]: i for i in p["items"]}
    assert by["Dana Cho"]["days_overdue"] == 2 and by["Omar Ali"]["days_overdue"] == 0
    assert by["Dana Cho"]["interview_id"] and by["Dana Cho"]["candidate_url"].endswith(f"/candidates/{a}")
    assert by["Dana Cho"]["at"] < by["Omar Ali"]["at"]
    assert "Priya Shah owes 2" in text(res)


def test_find_duplicates_gives_each_person_enough_for_a_card(store):
    a = store.add_candidate(name="Dana Cho", email="dana@x.com", current_title="Engineer", current_company="Acme",
                            location="Austin", source="LinkedIn")["id"]
    b = store.add_candidate(name="Dana Cho", email="d.cho@home.com", current_company="Acme")["id"]
    res = call(build_server(store), "find_duplicates")
    d = data(res)
    group = d["groups"][0]
    assert group["reasons"] and group["title"] == "Dana Cho, 2 profiles" and "/merge/" in group["compare_and_merge_url"]
    person = next(p for p in group["people"] if p["id"] == a)
    assert person["created_at"] and person["app_url"].endswith(f"/candidates/{a}") and person["current_title"] == "Engineer"
    assert b in {p["id"] for p in group["people"]} and d["app_url"].endswith("/candidates/duplicates")


def test_a_wrong_interview_id_says_how_to_find_the_right_one(store):
    with pytest.raises(Exception) as e:
        call(build_server(store), "add_interview_feedback", interview_id=999, feedback="Strong", verdict="yes")
    assert "list_interviews" in str(e.value) and "999" in str(e.value)


@pytest.mark.parametrize("word", ["lean yes", "lean_yes", "Lean Yes", "lean no", "lean_no", "strong_yes", "strong yes"])
def test_verdict_words_skills_use_are_accepted(store, word):
    jid = _job(store)
    a = _person(store, jid, "Dana Cho")
    iv = store.log(a, "interview", "Panel", at=_ago(30), job_id=jid, interviewer="Priya Shah")
    res = call(build_server(store), "add_interview_feedback", interview_id=iv["id"], feedback="Solid", interviewer="Priya",
               verdict=word)
    assert "Saved feedback" in text(res)
    want = word.lower().replace(" ", "_")
    assert interviews.verdicts(store.get_interview(iv["id"]))["priya shah"]["verdict"] == want


def test_the_views_use_real_controls_and_ask_claude_instead_of_writing():
    js = view_html("interviews")
    for word in ("Record feedback", "Reschedule", "Cancel", "Open in Talent Search", "Clash", "Feedback owed", "Today", "Tomorrow",
                 "lean yes", "lean no", "strong yes", "add_interview_feedback", "update_interview", "aria-pressed"):
        assert word in js, word
    owed = view_html("owed")
    assert "Copy nudge" in owed and "Log feedback" in owed and "Mark chased" in owed and "could you add your feedback on" in owed
    dup = view_html("duplicates")
    assert "Compare and merge in Talent Search" in dup and "Not the same people" in dup
    for h in (js, owed, dup):
        assert 'callTool("add_' not in h and 'callTool("update_' not in h  # views never write directly
