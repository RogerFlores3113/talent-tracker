"""Cycle 5 re-review, fixer K: Shortlist cards that survive a restart, plain card errors, work history on a new
title, Keep in Claude on Changes, tag case, and plain tool text."""
import json

import anyio
import pytest
from mcp.types import ClientCapabilities, Implementation, InitializeRequestParams

from talent import mcp_server, paths, shortlist
from talent.mcp_server import CARD_STALE, NOT_FROM_VIEW, VIEW_KEY, apps_support, build_server
from talent.mcp_views import view_html

from conftest import local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res):
    return "\n".join(c.text for c in res.content)


def key_of(server, res):
    """The key the Shortlist card asks for with its app-only tool (cycle 6 A: never in a result the model sees)."""
    d = res.structuredContent or {}
    ids = [p["id"] for p in d.get("added") or d.get("prospects") or [] if p.get("status", "proposed") == "proposed"]
    out = call(server, "shortlist_card_key", job_id=res.meta[VIEW_KEY]["job_id"], prospect_ids=ids)
    return out.structuredContent["key"]


@pytest.fixture(autouse=True)
def _apps_host(monkeypatch):
    """These tests play a host that says it shows MCP Apps views (cycle 6 A: only those get the card tools)."""
    monkeypatch.setattr(mcp_server, "_client_apps", lambda mcp: True)


def propose(server, job_id, *people):
    return call(server, "propose_candidates", job_id=job_id, people=list(people))


# ------------------------------------------------------- 1. keys survive a restart
def test_a_card_still_works_after_a_restart_with_a_key_from_the_new_session(store):
    # Cycle 6 F: keys are one-time and belong to the session that gave them out. The card asks for one before
    # each press, so after a restart it simply gets one from the new session; an old session's key is refused.
    job = store.add_job("Backend", company="Ledgerly")
    server = build_server(store)
    res = propose(server, job["id"], {"name": "Ana Ruiz", "linkedin_url": "https://linkedin.com/in/ana"})
    pid = res.structuredContent["added"][0]["id"]
    later = build_server(store)
    old = call(later, "pass_prospect", job_id=job["id"], prospect_id=pid, view_key=key_of(server, res))
    assert old.isError and text(old) == mcp_server.CARD_OTHER_SESSION
    out = call(later, "pass_prospect", job_id=job["id"], prospect_id=pid, view_key=key_of(later, res))
    assert not out.isError and text(out).startswith('Passed on "Ana Ruiz".')


def test_a_key_covers_only_the_batches_its_card_shows(store):
    job = store.add_job("Backend")
    server = build_server(store)
    first = propose(server, job["id"], {"name": "Ana Ruiz", "linkedin_url": "https://linkedin.com/in/ana"})
    later = propose(server, job["id"], {"name": "Bo Li", "linkedin_url": "https://linkedin.com/in/bo"})
    bo = later.structuredContent["added"][0]["id"]
    out = call(server, "keep_prospect", job_id=job["id"], prospect_id=bo, view_key=key_of(server, first))
    assert out.isError and text(out) == CARD_STALE and store.count_candidates() == 0
    # The full Shortlist card covers everyone waiting on it.
    listing = call(server, "list_prospects", job_id=job["id"])
    assert not call(server, "keep_prospect", job_id=job["id"], prospect_id=bo, view_key=key_of(server, listing)).isError


def test_a_wrong_key_reads_as_an_old_card_and_no_key_as_the_model(store):
    job = store.add_job("Backend")
    server = build_server(store)
    res = propose(server, job["id"], {"name": "Ana Ruiz", "linkedin_url": "https://linkedin.com/in/ana"})
    pid = res.structuredContent["added"][0]["id"]
    scope = key_of(server, res).rpartition(":")[0]
    for bad in ("old-session-key", f"{scope}:{'0' * 32}", key_of(server, res)[:-1]):
        out = call(server, "keep_prospect", job_id=job["id"], prospect_id=pid, view_key=bad)
        assert out.isError and text(out) == CARD_STALE
    assert text(call(server, "pass_prospect", job_id=job["id"], prospect_id=pid)) == NOT_FROM_VIEW
    assert "Error executing tool" not in CARD_STALE and "\u2014" not in CARD_STALE
    assert shortlist.counts(store, job["id"])["proposed"] == 1


def test_a_refused_press_is_plain_text_on_the_card(store):
    job = store.add_job("Backend")
    server = build_server(store)
    res = propose(server, job["id"], {"name": "Tom Becker", "linkedin_url": "https://linkedin.com/in/tom"})
    pid = res.structuredContent["added"][0]["id"]
    call(server, "pass_prospect", job_id=job["id"], prospect_id=pid, view_key=key_of(server, res))
    again = call(server, "keep_prospect", job_id=job["id"], prospect_id=pid, view_key=key_of(server, res))
    assert again.isError and text(again) == "Tom Becker was already passed."
    assert 'replace(/^Error executing tool [\\w-]+: /,"")' in view_html("shortlist")


def _params(**caps):
    return InitializeRequestParams(protocolVersion="2025-06-18", clientInfo=Implementation(name="x", version="1"),
                                   capabilities=ClientCapabilities(**caps))


def test_card_tools_are_hidden_from_hosts_that_say_they_show_no_views(store, monkeypatch):
    ui = {mcp_server.UI_EXTENSION: {"mimeTypes": ["text/html;profile=mcp-app"]}}
    assert apps_support(_params(extensions=ui)) is True
    assert apps_support(_params(experimental=ui)) is True
    assert apps_support(_params(extensions={})) is False
    assert apps_support(_params()) is None and apps_support(None) is None
    job = store.add_job("Backend")
    server = build_server(store)
    res = propose(server, job["id"], {"name": "Ana Ruiz", "linkedin_url": "https://linkedin.com/in/ana"})
    pid = res.structuredContent["added"][0]["id"]
    names = {t.name for t in anyio.run(server.list_tools)}
    assert {"keep_prospect", "pass_prospect", "shortlist_card_key"} <= names
    key = key_of(server, res)
    # Cycle 6 A: a host that says nothing (None) is treated like one that shows no views.
    for says in (False, None):
        monkeypatch.setattr(mcp_server, "_client_apps", lambda mcp, says=says: says)
        names = {t.name for t in anyio.run(server.list_tools)}
        assert not names & {"keep_prospect", "pass_prospect", "shortlist_card_key"} and "propose_candidates" in names
        out = call(server, "keep_prospect", job_id=job["id"], prospect_id=pid, view_key=key)
        assert out.isError and text(out) == NOT_FROM_VIEW
        asked = call(server, "shortlist_card_key", job_id=job["id"], prospect_ids=[pid])
        assert asked.isError and "key" not in (asked.structuredContent or {})


# ------------------------------------------------------- 3. work history
def test_a_new_title_at_the_same_company_ends_the_old_entry(store):
    server = build_server(store)
    call(server, "save_candidate", name="Rafael Gomez", email="r@x.com", current_title="Staff Engineer",
         current_company="Ledgerly", work_history=[{"title": "Staff Engineer", "company": "Ledgerly", "start": "2022"}])
    cid = store.find_duplicate("r@x.com")["id"]
    call(server, "save_candidate", name="Rafael Gomez", email="r@x.com", current_title="Principal Engineer")
    hist = store.work_history(cid)
    assert [(h["title"], h["company"]) for h in hist] == [("Principal Engineer", "Ledgerly"), ("Staff Engineer", "Ledgerly")]
    assert hist[0]["end"] is None and hist[1]["end"] and hist[1]["start"] == "2022" and hist[0]["start"] == hist[1]["end"]
    # The same title again changes nothing; a blank company on the current entry is filled in place.
    call(server, "save_candidate", name="Rafael Gomez", email="r@x.com", current_title="principal engineer")
    assert len(store.work_history(cid)) == 2
    lee = store.add_candidate([{"title": "SRE"}], name="Lee Park", email="l@x.com", current_title="SRE")
    call(server, "save_candidate", name="Lee Park", email="l@x.com", current_company="Ramp")
    assert [(h["title"], h["company"]) for h in store.work_history(lee["id"])] == [("SRE", "Ramp")]


# ------------------------------------------------------- 4. Changes, tags, text
def test_a_keep_in_claude_that_fills_a_saved_person_shows_in_claude_on_changes(store):
    job = store.add_job("Backend")
    emily = store.add_candidate(name="Emily Stone", current_title="SWE")
    server = build_server(store)
    res = propose(server, job["id"], {"name": "Emily Stone", "linkedin_url": "https://linkedin.com/in/emily-s"})
    pid = res.structuredContent["added"][0]["id"]
    out = call(server, "keep_prospect", job_id=job["id"], prospect_id=pid, view_key=key_of(server, res),
               candidate_id=emily["id"])
    assert not out.isError and store.get_candidate(emily["id"])["linkedin_url"]
    change = store._row("SELECT data FROM timeline WHERE candidate_id = ? AND kind = 'change'", (emily["id"],))
    assert json.loads(change["data"])["via"] == "claude"
    page = local_client(store).get("/changes").text
    assert "You, on the Shortlist card in your AI app" in page  # cycle 6 F wording


def test_skills_keep_the_case_given(store):
    store.add_candidate(name="A", tags="go, kafka, postgresql")
    job = store.add_job("Backend")
    server = build_server(store)
    res = propose(server, job["id"], {"name": "Rafael Gomez", "linkedin_url": "https://linkedin.com/in/rafael",
                                      "tags": ["Go", "Kafka", "PostgreSQL"]})
    pid = res.structuredContent["added"][0]["id"]
    out = call(server, "keep_prospect", job_id=job["id"], prospect_id=pid, view_key=key_of(server, res))
    assert store.get_candidate(out.structuredContent["candidate_id"])["tags"] == ["Go", "Kafka", "PostgreSQL"]
    assert store.search_candidates(tag="KAFKA").total == 2


def test_write_tools_answer_in_plain_sentences_without_json(store):
    job = store.add_job("Backend", company="Acme")
    ana = store.add_candidate(name="Ana Ruiz")
    server = build_server(store)
    res = propose(server, job["id"], {"name": "Bo Li", "linkedin_url": "https://linkedin.com/in/bo"})
    assert "{" not in text(res) and res.structuredContent["added"]
    res = call(server, "log_activity", candidate_id=ana["id"], kind="interview", summary="Onsite",
               when="2030-01-05 1:00 PM PT")
    assert "{" not in text(res) and f"interview id is {res.structuredContent['logged']['id']}" in text(res)
    res = call(server, "update_client", client="acme", update_day="friday", time_zone="PT")
    assert "{" not in text(res) and res.structuredContent["changed"] == ["time_zone", "update_day"]
    res = call(server, "mark_update_sent", client="acme")
    assert "{" not in text(res).split("\n")[0] and "\n{" not in text(res) and res.structuredContent["update_id"]
    tool = {t.name: t for t in anyio.run(server.list_tools)}["update_client"]
    assert "Set several fields in one call" in tool.description and "(one field per call)" not in tool.description
