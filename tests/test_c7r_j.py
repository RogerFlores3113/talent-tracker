"""Cycle 7 re-review, fixer J: live app_url port, big chase collapse and import day, same name with nothing to
compare, one "enough people" number, own-company wording, Ops review colours."""
from datetime import datetime, timedelta, timezone

import anyio
import pytest

from talent import actions, chase, mcp_server, opsreview, paths, plan, wording
from talent.mcp_server import build_server

from conftest import local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def _at(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _age(store, cid, jid, days):
    with store._lock, store._conn:
        store._conn.execute("UPDATE applications SET stage_changed_at = ? WHERE candidate_id = ? AND job_id = ?",
                            (_at(-days), cid, jid))


@pytest.fixture(autouse=True)
def _apps_host(monkeypatch):
    monkeypatch.setattr(mcp_server, "_client_apps", lambda mcp: True)


def test_app_url_follows_a_port_written_after_the_server_started(store):
    server = build_server(store)
    paths.port_file().write_text("8943")
    res = call(server, "search_candidates", query="zzz")
    assert res.structuredContent["app_url"].startswith("http://127.0.0.1:8943/")
    paths.port_file().write_text("9100")
    res = call(server, "search_candidates", query="zzz")
    assert res.structuredContent["app_url"].startswith("http://127.0.0.1:9100/")


def _crowd(store, n, import_days=None):
    job = store.add_job("SRE", company="Globex", stages=actions.stage_set("agency"), hiring_manager="Pat Lee")
    for i in range(n):
        cid = store.add_candidate(name=f"Waiter {i:03}")["id"]
        store.add_to_pipeline(cid, job["id"], "Submitted")
        _age(store, cid, job["id"], 200)
        if import_days is not None:
            with store._lock, store._conn:
                store._conn.execute(
                    "UPDATE timeline SET data = '{\"imported\": true}', source = 'CSV import', created_at = ?"
                    " WHERE candidate_id = ? AND job_id = ? AND kind = 'stage'", (_at(-import_days), cid, job["id"]))
    return job


def test_a_big_chase_is_one_line_per_client_on_today(store):
    _crowd(store, 30)
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "client_chase"]
    assert len(items) == 1
    assert items[0]["title"].startswith("Globex: 30 waiting on feedback") and "Open the chase list" in items[0]["title"]
    assert "—" not in items[0]["title"]


def test_a_small_chase_keeps_its_names(store):
    _crowd(store, 25)
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "client_chase"]
    assert len(items) == 1 and items[0]["title"].startswith("Chase Pat Lee at Globex")


def test_waits_count_from_the_import_day_unless_unticked(store):
    _crowd(store, 6, import_days=2)  # imported two days ago with a stage date 200 days old
    assert chase.from_import_day(store) is True
    assert chase.chase_list(store, days=5) == []  # only two days since the import
    chase.set_from_import_day(store, False)
    g = chase.chase_list(store, days=5)
    assert len(g) == 1 and len(g[0]["people"]) == 6 and g[0]["people"][0]["days"] >= 199
    chase.set_from_import_day(store, True)
    assert len(chase.chase_list(store, days=1)[0]["people"]) == 6  # past the import day plus a day: counted again


def test_settings_page_saves_the_import_day_choice(store):
    c = local_client(store)
    assert "Count waits from my import day" in c.get("/settings").text
    c.post("/settings", data={"flags_shown": "1"}, follow_redirects=False)
    assert chase.from_import_day(store) is False
    c.post("/settings", data={"flags_shown": "1", "chase_from_import": "1"}, follow_redirects=False)
    assert chase.from_import_day(store) is True


def test_same_name_with_nothing_to_compare_is_saved_as_new(store):
    store.add_candidate(name="Hana Khan")
    store.add_candidate(name="Hana Khan")
    server = build_server(store)
    res = call(server, "save_candidate", name="Hana Khan", email="hana@new.example")
    assert res.structuredContent["created"] is True
    text = "\n".join(c.text for c in res.content)
    assert ("Same name as 2 saved people with no email or LinkedIn to compare: saved as someone new. "
            "Merge them if they're the same person.") in text
    res = call(server, "save_candidate", name="Hana Khan")  # nothing to compare either way: still asks
    assert res.structuredContent["needs_confirmation"] is True


def test_one_enough_people_number_for_plan_and_today(store):
    job = store.add_job("Founding engineer", stages=actions.stage_set("agency"))
    actions.set_inhouse(store, True)
    assert plan.enough_people(store, job, 5) == 3  # no seat: the Settings number
    plan.save(store, role="Founding engineer", job_id=job["id"], target_start=(datetime.now(timezone.utc)
                                                                                    + timedelta(days=60)).date().isoformat())
    assert plan.enough_people(store, job, 5) == plan.SEAT_AIM
    thin = [i for i in actions.action_items(store)["items"] if i["kind"] == "thin"]
    assert thin and f"at least {plan.SEAT_AIM}" in thin[0]["title"]
    seat = plan.score(store, plan.seats(store)[0])
    assert any(f"Aim for at least {plan.SEAT_AIM}" in t for t in seat["todo"])


def test_own_company_replies_say_you_keep_or_pass():
    text = wording.rewrite("The recruiter keeps or passes them in the app.")
    assert text == "You keep or pass them in the app."
    assert "recruiter" not in wording.rewrite("Skipped: ask the recruiter what they are called")


def test_ops_review_colours_days_since_update(store, monkeypatch):
    from talent import reports
    sre = store.add_job("SRE", company="Globex", stages=actions.stage_set("agency"))
    assert opsreview.review(store)["rows"][0]["update_tone"] == ""  # never sent
    for days, tone in [(3, ""), (8, "warn"), (15, "overdue")]:
        monkeypatch.setattr(reports, "last_sent", lambda s, c, d=days: _at(-d))
        assert opsreview.review(store)["rows"][0]["update_tone"] == tone
    monkeypatch.setattr(reports, "last_sent", lambda s, c: _at(-15))
    html = local_client(store).get("/clients/review").text
    assert sre["title"] in html and 'class="badge overdue">15<' in html
