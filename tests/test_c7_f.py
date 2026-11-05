"""Cycle 7, fixer F: AI safety. Card keys the model can't mint, hourly caps on new people, report_problem scrubbing,
http(s)-only links, reasons for lost-stage moves, and a trust level for each AI app."""
import json

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from talent import ai_caps, backups, mcp_server, paths, recipes, review, sessions, trust
from talent.mcp_server import build_server
from talent.store import Store

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res):
    return "\n".join(c.text for c in res.content)


def _data(res):
    d = res[1] if isinstance(res, tuple) else getattr(res, "structuredContent", res)
    return d.get("result", d)


def person(n):
    return {"name": f"Person{chr(65 + n // 26)}{chr(65 + n % 26)} Smith", "email": f"p{n}@example.com"}


@pytest.fixture
def apps(monkeypatch):
    monkeypatch.setattr(mcp_server, "_client_apps", lambda mcp: True)


def _shortlist(store, *names):
    job = store.add_job("Backend", company="Ledgerly")
    server = build_server(store)
    res = call(server, "propose_candidates", job_id=job["id"],
               people=[{"name": n, "linkedin_url": f"https://linkedin.com/in/{n.split()[0].lower()}"} for n in names])
    return server, job, [a["id"] for a in res.structuredContent["added"]]


def key(server, job_id, ids):
    out = call(server, "shortlist_card_key", job_id=job_id, prospect_ids=ids)
    return None if out.isError else out.structuredContent["key"]


# ------------------------------------------------------------ 1. card keys
def test_no_card_key_for_ids_the_server_never_showed(store, apps):
    server, job, (ana, bo) = _shortlist(store, "Ana Ruiz", "Bo Li")
    other = store.add_job("Platform")
    sneaky = call(server, "shortlist_card_key", job_id=other["id"], prospect_ids=[ana, 9999])
    assert sneaky.isError and "hasn't shown that Shortlist" in text(sneaky)
    made_up = call(server, "shortlist_card_key", job_id=job["id"], prospect_ids=[9999])
    assert made_up.isError
    # ids it named that were not shown are left out of a key that is issued
    k = key(server, job["id"], [ana, 9999])
    out = call(server, "keep_prospects", job_id=job["id"], prospect_ids=[ana, 9999], view_key=k)
    assert k is not None and not out.isError or "key" in text(out)


def test_a_card_key_waits_a_moment_before_it_works(store, apps, monkeypatch):
    server, job, (ana,) = _shortlist(store, "Ana Ruiz")
    monkeypatch.setattr(mcp_server, "CARD_MIN_GAP", 60)
    k = key(server, job["id"], [ana])
    fast = call(server, "keep_prospect", job_id=job["id"], prospect_id=ana, view_key=k)
    assert fast.isError and text(fast) == mcp_server.CARD_TOO_FAST and store.count_candidates() == 0
    monkeypatch.setattr(mcp_server, "CARD_MIN_GAP", 0)  # the same key still works once the gap has passed
    assert not call(server, "keep_prospect", job_id=job["id"], prospect_id=ana, view_key=k).isError


def test_card_presses_have_an_hourly_limit(store, apps, monkeypatch):
    monkeypatch.setattr(ai_caps, "PRESS_CAP", 2)
    server, job, (a, b, c) = _shortlist(store, "Ana Ruiz", "Bo Li", "Cy Park")
    for pid in (a, b):
        assert not call(server, "pass_prospect", job_id=job["id"], prospect_id=pid, view_key=key(server, job["id"], [pid])).isError
    out = call(server, "pass_prospect", job_id=job["id"], prospect_id=c, view_key=key(server, job["id"], [c]))
    assert out.isError and text(out) == mcp_server.CARD_LIMIT


# ------------------------------------------------------------ 2. caps on new people
def test_the_ai_can_save_only_so_many_new_people_an_hour(store, monkeypatch):
    server = build_server(store)
    monkeypatch.setattr(ai_caps, "PEOPLE_CAP", 30)
    call(server, "save_candidates", people=[person(n) for n in range(20)])
    assert store.count_candidates() == 20
    with pytest.raises(ToolError, match="can add 30 new people an hour and has 10 left"):
        call(server, "save_candidates", people=[person(n) for n in range(20, 40)])
    assert store.count_candidates() == 20  # nothing from the refused call
    call(server, "save_candidates", people=[person(n) for n in range(20, 30)])
    with pytest.raises(ToolError, match="has 0 left"):
        call(server, "save_candidate", name="One More", email="more@example.com")
    # a person who is already saved merges, which adds nobody
    call(server, "save_candidate", name="PersonAA Smith", email="p0@example.com", current_title="CTO")


def test_keep_all_counts_by_size_and_pass_has_its_own_cap(store, apps, monkeypatch):
    server, job, ids = _shortlist(store, "Ana Ruiz", "Bo Li", "Cy Park")
    monkeypatch.setattr(ai_caps, "PEOPLE_CAP", 2)
    out = call(server, "keep_prospects", job_id=job["id"], prospect_ids=ids, view_key=key(server, job["id"], ids))
    assert out.isError and "can add 2 new people an hour" in text(out) and store.count_candidates() == 0
    monkeypatch.setattr(ai_caps, "PASS_CAP", 2)
    out = call(server, "pass_prospects", job_id=job["id"], prospect_ids=ids, view_key=key(server, job["id"], ids))
    assert out.isError and "can pass on 2 people an hour" in text(out)
    two = ids[:2]
    assert not call(server, "pass_prospects", job_id=job["id"], prospect_ids=two, view_key=key(server, job["id"], two)).isError


def test_changes_shows_what_is_left_of_the_ai_allowance(store):
    server = build_server(store)
    call(server, "save_candidate", name="Ana Ruiz", email="ana@example.com")
    page = local_client(store).get("/changes").text
    assert "add 39 of 40 new people" in page and "replace or clear 30 of 30" in page
    assert ai_caps.allowance(store)["people_left"] == 39


# ------------------------------------------------------------ 3. report_problem
def test_report_problem_keeps_a_short_summary_and_scrubs_names_and_addresses(store):
    server = build_server(store)
    res = call(server, "report_problem", summary="Jordan Smithers at 1234 Elm Street and Zhao Wen broke it. "
               "Call 415 555 0100 or mail zhao@example.com. " + "x" * 400, area="saving people")
    d = _data(res)
    shown = d["preview"]["title"] + d["preview"]["body"] + d["url"]
    for private in ("Jordan", "Smithers", "Elm", "Zhao", "415", "zhao@"):
        assert private not in shown, private
    assert "Area: saving people" in d["preview"]["body"]
    assert len(d["preview"]["body"]) < 700
    with pytest.raises(ToolError, match="doesn't take details"):
        call(server, "report_problem", summary="x", details="a long story")
    d2 = call(server, "report_problem", summary="Talent Search Settings page is slow", area="nonsense")
    d2 = _data(d2)
    assert "Talent Search Settings page is slow" in d2["preview"]["title"] and "Area: other" in d2["preview"]["body"]


# ------------------------------------------------------------ 4. links
def test_a_website_must_be_http_or_https(store):
    server = build_server(store)
    for bad in ("javascript:alert(1)", "data:text/html,hi", "//evil.example/x", "ftp://x.example"):
        with pytest.raises(ToolError, match="isn't a web address"):
            call(server, "save_candidate", name="Ana Ruiz", website=bad)
    assert store.count_candidates() == 0
    ok = call(server, "save_candidate", name="Ana Ruiz", website="ana.dev")
    assert (ok.structuredContent or {})["website"] == "https://ana.dev"
    page = local_client(store).post(f"/candidates/{ok.structuredContent['id']}", data={"name": "Ana Ruiz", "website": "javascript:alert(1)"})
    assert "isn&#39;t a web address" in page.text or "isn't a web address" in page.text
    assert store.get_candidate(ok.structuredContent["id"])["website"] == "https://ana.dev"


# ------------------------------------------------------------ 5. lost and closed-out stages
def _pipeline(store, stage="Screening"):
    job = store.add_job("Backend", company="Ledgerly", stages=["Sourced", "Screening", "Hired", "Rejected"])
    c = store.add_candidate(name="Lee Chen")
    store.add_to_pipeline(c["id"], job["id"], stage)
    return job, c, build_server(store)


def test_a_move_into_a_lost_stage_needs_a_reason(store):
    job, c, server = _pipeline(store)
    with pytest.raises(ToolError, match="Ask the recruiter why"):
        call(server, "move_stage", candidate_id=c["id"], job_id=job["id"], stage="Rejected")
    assert store.candidate_pipelines(c["id"])[0]["stage"] == "Screening"
    out = call(server, "move_stage", candidate_id=c["id"], job_id=job["id"], stage="Rejected", notes="Role changed")
    assert out.structuredContent["stage"] == "Rejected"


def test_a_move_out_of_a_closed_out_stage_is_flagged_and_held_in_cautious_mode(store):
    job, c, server = _pipeline(store, "Rejected")
    out = call(server, "move_stage", candidate_id=c["id"], job_id=job["id"], stage="Screening")
    assert out.structuredContent["stage"] == "Screening" and "counts as out" in out.structuredContent["warning"]
    store.move_stage(c["id"], job["id"], "Rejected", by="app")
    review.set_cautious(store, True)
    held = call(server, "move_stage", candidate_id=c["id"], job_id=job["id"], stage="Screening")
    assert "pending_review" in held.structuredContent
    assert store.candidate_pipelines(c["id"])[0]["stage"] == "Rejected"


# ------------------------------------------------------------ 6. trust levels
@pytest.fixture
def named(monkeypatch):
    box = {"name": "Acme Chat"}
    monkeypatch.setattr(mcp_server, "_client_name", lambda mcp: box["name"])
    return box


def test_a_new_app_is_listed_and_trusted_by_default(store, named):
    server = build_server(store)
    call(server, "save_candidate", name="Ana Ruiz", email="ana@example.com")
    [app] = trust.apps(store)
    assert app["name"] == "Acme Chat" and app["trust"] == "trusted" and app["first_seen"] <= app["last_seen"]
    page = local_client(store).get("/settings").text
    assert "Acme Chat" in page and "Read only" in page and "Suggest only" in page


def test_read_only_apps_read_but_cannot_write(store, named):
    server = build_server(store)
    call(server, "search_candidates", query="x")
    trust.set_trust(store, trust.apps(store)[0]["id"], "read_only")
    with pytest.raises(ToolError, match="read only"):
        call(server, "save_candidate", name="Ana Ruiz", email="ana@example.com")
    assert store.count_candidates() == 0
    assert call(server, "search_candidates", query="x").isError is False


def test_suggest_only_apps_land_every_write_on_review_and_keep_runs_it_once(store, named):
    trust.set_new_apps(store, "suggest")
    server = build_server(store)
    out = call(server, "save_candidate", name="Ana Ruiz", email="ana@example.com")
    assert out.structuredContent["pending_review"]["id"] and store.count_candidates() == 0
    assert trust.apps(store)[0]["trust"] == "suggest"
    call(server, "save_candidate", name="Ana Ruiz", email="ana@example.com")  # sent twice: one row
    [row] = review.pending(store)
    assert row["kind"] == "tool_call" and "Save candidate" in row["summary"]
    c = local_client(store)
    assert "Suggest only" in c.get("/review").text and "Ana Ruiz" in c.get("/review").text
    r = c.post(f"/review/{row['id']}/keep", follow_redirects=False)
    assert store.count_candidates() == 1 and not review.pending(store)
    assert store.ai_actions()[0]["confirmed_at"], flash_of(r)  # the recruiter's own choice: Undo all AI leaves it
    again = c.post(f"/review/{row['id']}/keep", follow_redirects=False)
    assert store.count_candidates() == 1


def test_suggest_only_apps_can_still_propose_and_cards_still_work(store, named):
    trust.set_new_apps(store, "suggest")
    server = build_server(store)
    job = store.add_job("Backend")
    res = call(server, "propose_candidates", job_id=job["id"], people=[{"name": "Ana Ruiz"}])
    assert res.structuredContent["added"] and not review.pending(store)


def test_settings_can_change_an_apps_trust_and_new_apps(store, named):
    server = build_server(store)
    call(server, "search_candidates", query="x")
    [app] = trust.apps(store)
    c = local_client(store)
    r = c.post("/settings/ai-apps", data={f"trust_{app['id']}": "suggest", "new_apps": "suggest"}, follow_redirects=False)
    assert "1 AI app changed" in flash_of(r) and "New AI apps start at Suggest only" in flash_of(r)
    assert trust.apps(store)[0]["trust"] == "suggest" and trust.new_apps_level(store) == "suggest"
    named["name"] = "Other App"
    call(build_server(store), "search_candidates", query="x")  # another process, another app
    assert {a["name"]: a["trust"] for a in trust.apps(store)} == {"Acme Chat": "suggest", "Other App": "suggest"}
    c.post("/settings/ai-apps", data={f"trust_{app['id']}": "trusted"})
    assert trust.new_apps_level(store) == "trusted"


def test_migration_adds_the_apps_table_last(store):
    from talent.store import _MIGRATIONS
    assert store.schema_version == len(_MIGRATIONS)
    assert store._row("SELECT count(*) AS n FROM ai_apps")["n"] == 0


# ------------------------------------------------------------ 7. roll back and restore
def test_a_roll_back_that_undoes_nothing_takes_no_backup_and_keeps_the_first_result(store):
    server = build_server(store)
    call(server, "save_candidate", name="Ana Ruiz", email="ana@example.com")
    sid = sessions.listing(store)[0]["id"]
    first = sessions.roll_back(store, sid)
    assert first["backup"] and first["undone"]
    n = len(list(backups.backups_dir().glob("*.db")))
    second = sessions.roll_back(store, sid)
    assert second["backup"] is None and not second["undone"]
    assert len(list(backups.backups_dir().glob("*.db"))) == n
    assert sessions.get(store, sid)["rolled_back"]["undone"] == 1


def test_restoring_a_backup_keeps_the_current_cautious_options(tmp_path):
    s = Store(paths.db_path())
    try:
        s.add_candidate(name="Ana Ruiz")
        old = backups.backup_before_import(s, reason="manual")
        review.set_cautious(s, True)
        review.set_options(s, ai_hold_contacts=True)
        trust.touch(s, "Acme Chat")
        trust.set_trust(s, trust.apps(s)[0]["id"], "suggest")
        backups.restore(s, old.name)
        assert review.cautious(s) and review.hold_contacts(s)
        assert trust.apps(s)[0]["name"] == "Acme Chat" and trust.apps(s)[0]["trust"] == "suggest"
    finally:
        s.close()
