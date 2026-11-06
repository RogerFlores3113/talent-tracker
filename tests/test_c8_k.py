"""Cycle 8, fixer K: Suggest only can't use the card tools, honest wait text, checks at hold time, size cap."""
import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from talent import mcp_server, review, trust
from talent.mcp_server import VIEW_KEY, build_server

from conftest import local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


@pytest.fixture
def named(monkeypatch):
    box = {"name": "Acme Chat"}
    monkeypatch.setattr(mcp_server, "_client_name", lambda mcp: box["name"])
    monkeypatch.setattr(mcp_server, "_client_apps", lambda mcp: True)
    return box


def text(res):
    return "\n".join(c.text for c in res.content)


def test_the_card_bypass_fails_for_suggest_only_and_creates_nobody(store, named):
    trust.set_new_apps(store, "suggest")
    server = build_server(store)
    job = store.add_job("Backend")
    res = call(server, "propose_candidates", job_id=job["id"], people=[{"name": "Ana Ruiz"}])
    ids = [p["id"] for p in res.structuredContent["added"]]
    with pytest.raises(ToolError, match="Suggest only"):
        call(server, "shortlist_card_key", job_id=res.meta[VIEW_KEY]["job_id"], prospect_ids=ids)
    for tool, args in (("keep_prospects", {"job_id": job["id"], "prospect_ids": ids, "view_key": "x"}),
                       ("keep_prospect", {"job_id": job["id"], "prospect_id": ids[0], "view_key": "x"}),
                       ("pass_prospect", {"job_id": job["id"], "prospect_id": ids[0], "view_key": "x"}),
                       ("pass_prospects", {"job_id": job["id"], "prospect_ids": ids, "view_key": "x"})):
        with pytest.raises(ToolError, match="Shortlist page"):
            call(server, tool, **args)
    assert store.count_candidates() == 0 and not review.pending(store)


def test_a_trusted_app_keeps_the_card_tools(store, named):
    server = build_server(store)
    job = store.add_job("Backend")
    res = call(server, "propose_candidates", job_id=job["id"], people=[{"name": "Ana Ruiz"}])
    ids = [p["id"] for p in res.structuredContent["added"]]
    key = call(server, "shortlist_card_key", job_id=job["id"], prospect_ids=ids).structuredContent["key"]
    call(server, "keep_prospects", job_id=job["id"], prospect_ids=ids, view_key=key)
    assert store.count_candidates() == 1


def test_wait_text_is_true_with_cautious_off_and_on(store, named):
    trust.set_new_apps(store, "suggest")
    server = build_server(store)
    out = text(call(server, "save_candidate", name="Ana Ruiz", email="ana@example.com"))
    assert "Cautious mode" not in out and "Suggest only" in out and "Review" in out
    review.set_cautious(store, True)
    out = text(call(server, "save_candidate", name="Bo Lee", email="bo@example.com"))
    assert "Cautious mode is on" in out


def test_bad_requests_are_refused_at_hold_time(store, named):
    trust.set_new_apps(store, "suggest")
    server = build_server(store)
    job = store.add_job("Backend")
    bad = [("save_candidate", {"name": "Ana Ruiz", "bogus": 1}, "no bogus"),
           ("save_candidate", {"email": "a@b.co"}, "name"),
           ("set_followup_sequence", {"job_id": job["id"], "steps": "nope"}, "No change made"),
           ("set_followup_sequence", {"job_id": job["id"], "steps": [{"label": "First", "day": 0, "text": "Hi"}]},
            "at least one follow-up"),
           ("set_followup_sequence", {"job_id": 9999, "steps": []}, "No change made"),
           ("book_loop", {"job_id": job["id"], "candidate_id": 1}, "No change made")]
    for tool, args, want in bad:
        with pytest.raises(ToolError, match=want):
            call(server, tool, **args)
    assert not review.pending(store)


def test_a_valid_request_still_waits(store, named):
    trust.set_new_apps(store, "suggest")
    server = build_server(store)
    job = store.add_job("Backend")
    steps = [{"label": "First", "day": 0, "text": "Hi {first_name}"}, {"label": "Nudge", "day": 3, "text": "Bump"}]
    out = call(server, "set_followup_sequence", job_id=job["id"], steps=steps)
    assert out.structuredContent["pending_review"]["id"]
    assert len(review.pending(store)) == 1


def test_held_requests_have_a_size_cap(store, named):
    trust.set_new_apps(store, "suggest")
    server = build_server(store)
    job = store.add_job("Backend")
    with pytest.raises(ToolError, match="too long"):
        call(server, "add_suggestion", text="x" * 30_000)
    assert not review.pending(store)
    with pytest.raises(ValueError, match="too long"):
        trust.hold(store, "add_suggestion", {"text": "x" * 30_000}, None, "Acme Chat")
