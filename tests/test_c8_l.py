"""Cycle 8, fixer L: imported dates start the waiting clock and "new this week", hires against openings, the
offer-missing item and Offers out on imported history."""
from datetime import datetime, timedelta, timezone

import anyio
import pytest

from talent import actions, chase, desk, importers, mcp_server, offers, opsreview
from talent.mcp_server import build_server

from conftest import local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def _day(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


@pytest.fixture(autouse=True)
def _apps_host(monkeypatch):
    monkeypatch.setattr(mcp_server, "_client_apps", lambda mcp: True)


HEADER = ["Name", "Email", "Job", "Client", "Stage", "Stage Entered", "Created Date"]


def _import(store, rows, mapping=None):
    mapping = mapping or ["name", "email", "job_title", "job_company", "stage", "stage_changed_at", "created_at"]
    return importers.import_rows(store, HEADER, rows, mapping, add_stages=True)


def _agency_file(store):
    rows = [["Old Submit", "o@x.com", "SRE", "Acme", "Submitted", _day(158), _day(200)],
            ["Fresh Submit", "f@x.com", "SRE", "Acme", "Submitted", _day(7), _day(9)],
            ["Undated", "u@x.com", "SRE", "Acme", "Submitted", "", ""]]
    return _import(store, rows)


def test_import_dates_start_the_clock_and_say_how_they_were_read(store):
    r = _agency_file(store)
    assert "2 stage dates were read" in r["date_note"] and "1 had none" in r["date_note"]
    assert "2 created dates read" in r["date_note"]
    left: dict = {}
    groups = chase.chase_list(store, days=5, left_out=left)
    names = {p["name"]: p for g in groups for p in g["people"]}
    assert names["Fresh Submit"]["days"] in (6, 7)  # counted from its stage date, not the import day
    assert "Old Submit" not in names and left["too_old"] == 1  # 158 days: folded, not hidden
    assert left["older"][0]["count"] == 1 and left["older"][0]["longest"] >= 157 and left["max"] == chase.IMPORT_OLD_DAYS


def test_old_waits_are_folded_on_the_chase_page_and_the_limit_is_adjustable(store):
    _agency_file(store)
    c = local_client(store)
    html = c.get("/clients/chase").text
    assert "1 waiting more than 60 days" in html and "<details" in html
    chase.set_chase_max_days(store, 365)
    assert any(p["name"] == "Old Submit" for g in chase.chase_list(store, days=5) for p in g["people"])


def test_no_dates_keeps_todays_behaviour(store):
    r = _import(store, [["Plain", "p@x.com", "SRE", "Acme", "Submitted", "", ""]])
    assert "No stage dates were read" in r["date_note"]
    assert chase.chase_list(store, days=5) == []


def test_new_this_week_uses_the_created_date(store):
    _agency_file(store)
    data = opsreview.review(store)
    row = data["rows"][0]
    assert row["counts"]["new"] == 1  # created 9 and 200 days ago are not new; the undated one is
    assert row["counts"]["process"] == 3


def test_hires_over_openings_never_read_as_filled(store):
    job = store.add_job("Data Engineer", company="Acme", stages=actions.stage_set("agency"), openings=3)
    for i in range(8):
        cid = store.add_candidate(name=f"Hire {i}")["id"]
        store.add_to_pipeline(cid, job["id"], "Placed")
    text = actions.status_digest(store, "acme")
    assert "8 placed against 3 openings" in text and "3 of 3" not in text
    assert actions.over_openings(store)[0]["text"] == "8 placed against 3 openings"
    res = call(build_server(store), "status_digest", company="acme")
    assert any("Check openings" in c.text and "update_job" in c.text for c in res.content)
    assert res.structuredContent["hired_over_openings"][0]["hired"] == 8
    c = local_client(store)
    assert "8 hired against 3 openings" in c.get("/clients/review").text
    slug = desk.reports.client(store, "acme")["slug"]
    assert "8 placed against 3 openings" in c.get(f"/clients/{slug}/desk").text
    assert "Check openings" in call(build_server(store), "client_desk", client="acme").content[0].text


def test_openings_form_shows_the_hired_count(store):
    _import(store, [["Hired One", "h@x.com", "SRE", "Acme", "Hired", "", ""]])
    c = local_client(store)
    assert "1 already hired" in c.get("/import/openings").text


def _offer_import(store, n=5):
    rows = [[f"Offer {i}", f"o{i}@x.com", "SRE", "Acme", "Offer", "", ""] for i in range(n)]
    _import(store, rows)
    return store.list_jobs()[0]["id"]


def test_offer_missing_import_can_be_dismissed_for_the_job_in_one_step(store):
    jid = _offer_import(store)
    item = next(i for i in actions.action_items(store)["items"] if i["key"].startswith("offer_missing_import"))
    assert item["job_id"] == jid and item["dismiss"] and item["count"] == 5
    c = local_client(store)
    page = c.get("/").text
    assert "These are old, don&#39;t ask again" in page or "These are old, don't ask again" in page
    c.post("/actions/snooze", data={"key": item["key"], "days": "3600"})
    assert not [i for i in actions.action_items(store)["items"] if i["key"].startswith("offer_missing_import")]


def test_offers_out_counts_offer_stage_people_when_no_offer_is_recorded(store):
    jid = _offer_import(store)
    row = opsreview.review(store)["rows"][0]
    assert row["counts"]["offers"] == 5
    first = store.pipeline(jid)[0]["candidate_id"]
    offers.save(store, first, jid, status="drafted")
    assert opsreview.review(store)["rows"][0]["counts"]["offers"] == 4  # a recorded offer is the main source
