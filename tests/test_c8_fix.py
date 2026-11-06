"""Cycle 8 re-review fixes: unknown clients refused at hold time, true Review lead, the chase page and desk with a
snoozed nudge, and one story about openings that an import never set."""
import json
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from talent import actions, importers, mcp_server, reports, review, trust
from talent.mcp_server import build_server

from conftest import local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def _chasable(store):
    job = store.add_job("Analyst", company="Pinecrest Capital", stages=actions.stage_set("agency"))
    reports.save_client(store, "Pinecrest Capital", contacts="Dana Ortiz <dana@pinecrest.com>")
    cid = store.add_candidate(name="Lena Hoffmann")["id"]
    store.add_to_pipeline(cid, job["id"], "Submitted")
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    store._conn.execute("UPDATE applications SET stage_changed_at = ? WHERE candidate_id = ?", (old, cid))
    return job, cid


def test_a_suggest_only_mark_chased_for_an_unknown_client_is_refused_not_queued(store, monkeypatch):
    monkeypatch.setattr(mcp_server, "_client_name", lambda mcp: "Acme Chat")
    trust.set_new_apps(store, "suggest")
    server = build_server(store)
    with pytest.raises(ToolError, match="No change made: no client matches"):
        call(server, "mark_chased", client="nobody inc")
    assert not review.pending(store)


def test_review_lead_is_true_with_cautious_off(store):
    page = local_client(store).get("/review").text
    assert "These were sent while it was on" not in page and "Suggest only" in page


def test_the_chase_page_shows_when_a_snoozed_nudge_comes_back_and_drafts_nothing(store):
    _chasable(store)
    server = build_server(store)
    call(server, "mark_chased", client="pinecrest", snooze_days=5)
    c = local_client(store)
    page = c.get("/clients/chase").text
    assert "Chased " in page and ", next " in page and "Hi Dana" not in page
    desk = c.get("/clients/pinecrest-capital/desk").text
    assert "Chased Chased" not in desk and ", next " in desk


def test_openings_an_import_never_set_read_the_same_on_every_page(store):
    job = store.add_job("Data Engineer", company="Acme", stages=actions.stage_set("agency"))
    for i in range(3):
        cid = store.add_candidate(name=f"Hire {i}")["id"]
        store.add_to_pipeline(cid, job["id"], "Placed")
    store._conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                        (importers.OPENINGS_KEY, json.dumps([job["id"]])))
    assert actions.over_openings(store)[0]["text"] == "3 placed, openings not set yet"
    review_page = local_client(store).get("/clients/review").text
    assert "openings not set yet" in review_page and "against 1 opening" not in review_page
