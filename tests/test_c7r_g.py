"""Cycle 7 re-review, fixer G: renaming doesn't escape trust, card tools honour trust, hourly caps on proposing and
creating, a failed Keep keeps waiting, held arguments in full, a queue cap per app, stricter problem reports."""
import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from talent import ai_caps, mcp_server, recipes, review, trust
from talent.mcp_server import build_server

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


def level(store, name):
    return next(a["trust"] for a in trust.apps(store) if a["name"] == name)


def test_names_are_compared_without_case_or_spacing_and_empty_is_one_row(store, named):
    call(build_server(store), "search_candidates", query="x")
    named["name"] = "  acme   CHAT "
    call(build_server(store), "search_candidates", query="x")
    for n in ("", None, "   "):
        named["name"] = n
        call(build_server(store), "search_candidates", query="x")
    assert sorted(a["name"] for a in trust.apps(store)) == ["Acme Chat", "Unnamed app"]


def test_renaming_in_one_process_keeps_the_first_apps_level(store, named):
    server = build_server(store)
    call(server, "search_candidates", query="x")
    trust.set_trust(store, trust.apps(store)[0]["id"], "read_only")
    for n in ("claude desktop", "Codex", ""):
        named["name"] = n
        with pytest.raises(ToolError, match="read only"):
            call(server, "save_candidate", name="Ana Ruiz", email="ana@example.com")
    assert store.count_candidates() == 0


def test_a_new_name_waits_once_any_app_is_restricted(store, named):
    call(build_server(store), "search_candidates", query="x")
    assert level(store, "Acme Chat") == "trusted"
    trust.set_trust(store, trust.apps(store)[0]["id"], "read_only")
    named["name"] = "Codex"
    out = call(build_server(store), "save_candidate", name="Ana Ruiz", email="ana@example.com")
    assert level(store, "Codex") == "suggest" and out.structuredContent["pending_review"]["id"]
    assert store.count_candidates() == 0


def test_settings_shows_first_seen_writes_and_new(store, named):
    server = build_server(store)
    call(server, "save_candidate", name="Ana Ruiz", email="ana@example.com")
    [app] = trust.apps(store)
    assert app["writes"] == 1 and app["is_new"]
    page = local_client(store).get("/settings").text
    assert ">New<" in page and "Changes made" in page and "already set to Read only" in page


def test_card_tools_refuse_a_read_only_app_and_cap_a_suggest_only_app(store, named):
    server = build_server(store)
    job = store.add_job("Backend")
    call(server, "search_candidates", query="x")
    trust.set_trust(store, trust.apps(store)[0]["id"], "read_only")
    for tool, args in (("shortlist_card_key", {"job_id": job["id"], "prospect_ids": [1]}),
                       ("pass_prospect", {"job_id": job["id"], "prospect_id": 1, "view_key": "k"}),
                       ("keep_prospect", {"job_id": job["id"], "prospect_id": 1, "view_key": "k"})):
        with pytest.raises(ToolError, match="read only"):
            call(server, tool, **args)
    trust.set_trust(store, trust.apps(store)[0]["id"], "suggest")  # Suggest only is refused them too (cycle 8)
    with pytest.raises(ToolError, match="Suggest only"):
        call(server, "pass_prospect", job_id=job["id"], prospect_id=1, view_key="k")


def test_hourly_caps_on_proposing_jobs_and_clients(store, named, monkeypatch):
    server = build_server(store)
    job = store.add_job("Backend")
    monkeypatch.setattr(ai_caps, "PROPOSE_CAP", 2)
    monkeypatch.setattr(ai_caps, "JOB_CAP", 1)
    monkeypatch.setattr(ai_caps, "CLIENT_CAP", 1)
    with pytest.raises(ToolError, match="propose 2 people"):
        call(server, "propose_candidates", job_id=job["id"], people=[{"name": f"Pat {c}"} for c in "ABC"])
    call(server, "propose_candidates", job_id=job["id"], people=[{"name": "Pat Aa"}, {"name": "Pat Bb"}])
    with pytest.raises(ToolError, match="propose 2 people"):
        call(server, "propose_candidates", job_id=job["id"], people=[{"name": "Pat Cc"}])
    call(server, "create_job", title="Designer")
    with pytest.raises(ToolError, match="create 1 jobs"):
        call(server, "create_job", title="Another")
    call(server, "create_client", name="Harbor Labs")
    with pytest.raises(ToolError, match="create 1 clients"):
        call(server, "create_client", name="Other Labs")
    page = local_client(store).get("/changes").text
    assert "propose 0 of 2 people" in page and "create 0 of 1 jobs" in page


def test_a_keep_whose_replay_fails_keeps_waiting_with_the_reason(store, named):
    trust.set_new_apps(store, "suggest")
    server = build_server(store)
    call(server, "save_candidate", name="Ana Ruiz", email="ana@example.com")
    [row] = review.pending(store)
    store._conn.execute("UPDATE ai_review SET payload = replace(payload, 'save_candidate', 'no_such_tool') WHERE id = ?",
                        (row["id"],))
    store._conn.commit()
    with pytest.raises(ValueError, match="Couldn't keep it"):
        review.keep(store, row["id"])
    [again] = review.pending(store)
    assert again["id"] == row["id"] and "Couldn't keep it" in again["result"]
    assert "Couldn&#39;t keep it" in local_client(store).get("/review").text or "Couldn't keep it" in local_client(store).get("/review").text


def test_long_held_arguments_show_the_count_and_a_show_all_toggle(store, named):
    trust.set_new_apps(store, "suggest")
    call(build_server(store), "save_candidate", name="Ana Ruiz", email="ana@example.com", about="word " * 400)
    [row] = review.pending(store)
    x = next(l for l in trust.lines(row) if l["label"] == "about")
    assert x["more"] == len(x["full"]) - trust.SHOW_CHARS and len(x["new"]) == trust.SHOW_CHARS
    page = local_client(store).get("/review").text
    assert f"and {x['more']} more characters" in page and "Show all" in page


def test_the_waiting_cap_is_per_app(store, named, monkeypatch):
    monkeypatch.setattr(review, "QUEUE_CAP", 2)
    for i in range(2):
        trust.hold(store, "close_job", {"job_id": 1, "n": i}, "s", "Noisy")
    with pytest.raises(ValueError, match="already wait"):
        trust.hold(store, "close_job", {"job_id": 1, "n": 9}, "s", "Noisy")
    assert trust.hold(store, "close_job", {"job_id": 1, "n": 9}, "s", "Quiet")["id"]


def test_problem_reports_drop_company_names_and_keep_plain_words():
    out = recipes.scrub_strict("Globex import fails in Talent Search when I press Keep. 12 rows.")
    assert "Globex" not in out and "Talent Search" in out and "Keep" in out and "12" in out


def test_keep_all_says_it_is_not_counted(store, named):
    trust.set_new_apps(store, "suggest")
    call(build_server(store), "save_candidate", name="Ana Ruiz", email="ana@example.com")
    assert "not counted in your AI app" in local_client(store).get("/review").text
