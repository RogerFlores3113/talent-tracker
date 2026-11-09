"""Cycle 7, fixer D: founder inputs (save and propose fields, offer parsing), own-company wording, hiring plan."""
from datetime import date, datetime, timedelta, timezone

import pytest

from talent import reports, actions, offers, plan, review, shortlist
from talent import mcp_server
from talent.mcp_server import build_server
from talent.store import _MIGRATIONS, as_local

from conftest import flash_of, local_client
from test_mcp import call, structured, text


def _today():
    return as_local(datetime.now(timezone.utc)).date()


def _iso(d):
    return d.isoformat()


# ----------------------------------------------------------- 1. save and propose inputs
def test_save_candidate_takes_headline_and_notes(store):
    server = build_server(store)
    res = call(server, "save_candidate", name="Dana Whitlock", email="d@x.com",
               headline="Staff Engineer at Stripe", notes="Led the payments ledger rewrite.")
    out = structured(res)
    c = store.find_duplicate("d@x.com")
    assert (c["current_title"], c["current_company"]) == ("Staff Engineer", "Stripe")
    assert c["about"] == "Led the payments ledger rewrite."
    assert "ignored_fields" not in out
    assert call(server, "search_candidates", query="payments ledger").structuredContent["total"] == 1


def test_notes_become_a_note_when_there_is_an_about_and_headline_never_overwrites_a_title(store):
    server = build_server(store)
    store.add_candidate(name="Dana Whitlock", email="d@x.com", about="Saved about", current_title="CTO")
    res = call(server, "save_candidate", name="Dana Whitlock", email="d@x.com", headline="Engineer at Acme",
               current_title="CTO", notes="Met at the meetup.")
    c = store.find_duplicate("d@x.com")
    assert c["about"] == "Saved about"
    notes = [t["body"] for t in store.timeline(c["id"]) if t["kind"] == "note"]
    assert "Met at the meetup." in notes
    assert "Did not use the headline" in text(res)
    call(server, "save_candidate", name="Dana Whitlock", email="d@x.com", notes="Met at the meetup.")
    assert [t["body"] for t in store.timeline(c["id"]) if t["kind"] == "note"].count("Met at the meetup.") == 1


def test_save_candidates_takes_headline_and_notes(store):
    server = build_server(store)
    res = call(server, "save_candidates", people=[{"name": "Lee Chen", "email": "l@x.com", "headline": "SRE at Fly",
                                                  "notes": "Great on call stories."}])
    c = store.find_duplicate("l@x.com")
    assert (c["current_title"], c["current_company"], c["about"]) == ("SRE", "Fly", "Great on call stories.")
    assert "Ignored fields" not in text(res)


def test_propose_takes_email_and_reason_and_keep_saves_the_email(store):
    server = build_server(store)
    job = store.add_job("Backend")
    res = call(server, "propose_candidates", job_id=job["id"], people=[
        {"name": "Ana Ruiz", "email": "ana@x.com", "reason": "Built a ledger at Stripe", "title": "Eng"},
        {"name": "Bo Li", "email": "not an email", "why": "Rust"}])
    out = structured(res)
    assert "ignored_fields" not in out
    ana = next(p for p in out["added"] if p["name"] == "Ana Ruiz")
    assert ana["fit"] == "Built a ledger at Stripe" and ana["email"] == "ana@x.com"
    bo = next(p for p in out["added"] if p["name"] == "Bo Li")
    assert "email" not in bo
    rec = shortlist.keep(store, job["id"], ana["id"])
    assert store.get_candidate(rec["candidate"]["id"])["email"] == "ana@x.com"


def test_keep_does_not_put_someone_elses_email_on_a_new_person(store):
    server = build_server(store)
    job = store.add_job("Backend")
    store.add_candidate(name="Someone Else", email="shared@x.com")
    res = call(server, "propose_candidates", job_id=job["id"], people=[{"name": "Ana Ruiz", "email": "shared@x.com"}])
    pid = structured(res)["added"][0]["id"]
    rec = shortlist.keep(store, job["id"], pid)
    assert rec["candidate"]["name"] == "Ana Ruiz" and not store.get_candidate(rec["candidate"]["id"])["email"]


# ----------------------------------------------------------- 2. record_offer
def _offer_setup(store):
    job = store.add_job("Founding Backend Engineer", company="Acme")
    m = store.add_candidate(name="Marcus Oyelaran")
    store.add_to_pipeline(m["id"], job["id"])
    return job, m


def test_money_text():
    assert offers.money_text(180000) == "180,000"
    assert offers.money_text("180k") == "180,000"
    assert offers.money_text("$1.5M") == "$1,500,000"
    assert offers.money_text("$185,000 base") == "$185,000 base"


def test_record_offer_takes_numbers_and_fuzzy_dates_and_echoes_the_day(store):
    job, m = _offer_setup(store)
    server = build_server(store)
    res = call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], salary=180000, bonus="20k",
               answer_by="Friday", start_date="Nov 13")
    o = offers.get(store, m["id"], job["id"])
    assert o["salary"] == "180,000" and o["bonus"] == "20,000"
    assert _iso(actions.parse_day("Friday", _today())) == o["answer_by"]
    assert text(res).count("I read the") == 1 and "answer-by date as" in text(res) and "start date as" in text(res)
    d = date.fromisoformat(o["start_date"])
    assert (d.month, d.day) == (11, 13)
    res = call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], answer_by="by next fri")
    assert offers.get(store, m["id"], job["id"])["answer_by"] == _iso(actions.parse_day("next fri", _today()))
    assert structured(res)["read_as"]["answer_by"]


def test_record_offer_aliases_and_status_error(store):
    job, m = _offer_setup(store)
    server = build_server(store)
    call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], base_salary=175000, deadline="2026-12-01")
    o = offers.get(store, m["id"], job["id"])
    assert o["salary"] == "175,000" and o["answer_by"] == "2026-12-01"
    with pytest.raises(Exception) as exc:
        call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], status="accepted")
    msg = str(exc.value)
    assert "drafted" in msg and "out" in msg and "record_offer_answer" in msg and "validation error" not in msg


# ----------------------------------------------------------- 3. own-company wording
def test_replies_say_you_for_a_founder_and_the_recruiter_for_an_agency(store):
    job, m = _offer_setup(store)
    server = build_server(store)
    agency = text(call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], salary="190k"))
    assert "The recruiter can undo it" in agency
    actions.set_inhouse(store, True)
    own = text(call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], salary="195k"))
    assert "You can undo it" in own and "recruiter" not in own
    listed = text(call(server, "list_offers"))
    assert "private to you" in listed and "recruiter" not in listed


def test_rewrite_keeps_candidate_text_and_verbs():
    from talent import wording

    assert wording.rewrite("Ask the recruiter to confirm. The recruiter has seen it.") == \
        "Ask the user to confirm. You have seen it."
    fenced = "ok the recruiter <data-ab12>the recruiter said hi</data-ab12>"
    assert wording.rewrite(fenced) == "ok you <data-ab12>the recruiter said hi</data-ab12>"


def test_nav_shows_the_plan_only_for_own_company(store):
    c = local_client(store)
    assert 'href="/plan"' not in c.get("/").text
    actions.set_inhouse(store, True)
    assert 'href="/plan"' in c.get("/").text and 'href="/clients"' in c.get("/").text


def test_thin_job_item_links_to_the_find_people_prompt(store):
    job = store.add_job("Backend", company="Acme")
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "thin"]
    assert items and items[0]["href"] == f"/jobs/{job['id']}?find=1#find-people"
    page = local_client(store).get(items[0]["href"].split("#")[0]).text
    assert 'id="find-people" open' in page


# ----------------------------------------------------------- 4. hiring plan
def _seat_job(store, n_screening=0, n_interview=0, n_offer=0, title="Backend"):
    job = store.add_job(title, company="Acme")
    stages = {s["outcome"]: s["name"] for s in store.job_stages(job["id"]) if s["outcome"] == "open"}
    names = [s["name"] for s in store.job_stages(job["id"]) if s["outcome"] == "open"]
    k = 0
    for count, stage in ((n_screening, "Screening"), (n_interview, "Interviewing"), (n_offer, "Offer")):
        for _ in range(count):
            k += 1
            c = store.add_candidate(name=f"Person {title} {k}")
            store.add_to_pipeline(c["id"], job["id"])
            store.move_stage(c["id"], job["id"], stage, by="app")
    return job


def test_migration_is_one_function_at_the_end(store):
    assert store.schema_version == len(_MIGRATIONS)
    assert store._row("SELECT count(*) AS n FROM plan_seats")["n"] == 0


def test_a_seat_with_nobody_close_to_its_start_is_behind(store):
    job = _seat_job(store, n_screening=1)
    soon = _today() + timedelta(days=30)
    s = plan.save(store, None, role="Founding backend", job_id=job["id"], target_start=_iso(soon))["seat"]
    row = plan.scoreboard(store)[0]
    assert row["id"] == s["id"] and row["state"] == "behind"
    assert row["next_step"]["step"] == "interview" or row["next_step"]["step"] == "offer"
    assert any("interview" in t.lower() or "offer" in t.lower() for t in row["todo"])
    assert row["funnel"]["groups"][1]["count"] == 1


def test_a_seat_far_from_its_start_is_on_track_and_dates_drive_tight(store):
    job = _seat_job(store, n_screening=3)
    far = _today() + timedelta(days=120)
    plan.save(store, None, role="A", job_id=job["id"], target_start=_iso(far))
    assert plan.scoreboard(store)[0]["state"] == "on_track"
    lead = plan.lead_days(store)
    tight = _today() + timedelta(days=lead["interview"] + 3)  # interviews are due in 3 days
    plan.save(store, None, role="B", job_id=job["id"], target_start=_iso(tight))
    assert plan.scoreboard(store)[1]["state"] == "tight"


def test_seat_states_filled_paused_nojob_nodate(store):
    job = _seat_job(store, n_offer=1)
    plan.save(store, None, role="Filled by hand", status="filled")
    plan.save(store, None, role="Paused", status="on hold")
    plan.save(store, None, role="No job", target_start=_iso(_today() + timedelta(days=90)))
    plan.save(store, None, role="No date", job_id=job["id"])
    states = [r["state"] for r in plan.scoreboard(store)]
    assert states == ["filled", "paused", "no_job", "no_date"]
    hired = store.job_stages(job["id"])
    won = next(s["name"] for s in hired if s["outcome"] == "hired")
    c = store.add_candidate(name="Hired Hal")
    store.add_to_pipeline(c["id"], job["id"])
    store.move_stage(c["id"], job["id"], won, by="app")
    assert plan.scoreboard(store)[3]["state"] == "filled"


def test_two_seats_on_one_job_split_its_hires(store):
    job = _seat_job(store)
    won = next(s["name"] for s in store.job_stages(job["id"]) if s["outcome"] == "hired")
    c = store.add_candidate(name="Hired Hal")
    store.add_to_pipeline(c["id"], job["id"])
    store.move_stage(c["id"], job["id"], won, by="app")
    far = _iso(_today() + timedelta(days=150))
    plan.save(store, None, role="One", job_id=job["id"], target_start=far)
    plan.save(store, None, role="Two", job_id=job["id"], target_start=far)
    assert [r["state"] for r in plan.scoreboard(store)] == ["filled", "on_track"]


def test_today_item_for_a_behind_seat(store):
    job = _seat_job(store, n_screening=1)
    plan.save(store, None, role="Founding backend", job_id=job["id"], target_start=_iso(_today() + timedelta(days=20)))
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "plan"]
    assert len(items) == 1 and "behind the plan" in items[0]["title"] and items[0]["href"] == "/plan"
    html = local_client(store).get("/").text
    assert "Hiring plan" in html and 'href="/plan"' in html


def test_plan_tools_read_and_write_and_undo(store):
    job = _seat_job(store, n_screening=2)
    server = build_server(store)
    names = {t.name for t in anyio_tools(server)}
    assert {"list_hiring_plan", "set_plan_seat"} <= names and not any("remove" in n and "seat" in n for n in names)
    empty = call(server, "list_hiring_plan")
    assert "empty" in text(empty)
    res = call(server, "set_plan_seat", role="Founding backend", job_id=job["id"], target_start="2027-03-01")
    out = structured(res)
    sid = out["seat_id"]
    assert plan.get(store, sid)["target_start"] == "2027-03-01"
    with pytest.raises(Exception, match="already has a seat"):
        call(server, "set_plan_seat", role="founding backend")
    res = call(server, "set_plan_seat", seat_id=sid, status="paused")
    assert plan.get(store, sid)["status"] == "paused" and "undo" in text(res)
    listed = call(server, "list_hiring_plan")
    assert "Founding backend" in text(listed) and structured(listed)["seats"][0]["state"] == "paused"
    acts = store.ai_actions()
    assert [a["kind"] for a in acts][:2] == ["plan_seat", "plan_seat"]
    store.undo_ai_action(acts[0]["id"])
    assert plan.get(store, sid)["status"] == "open"
    store.undo_ai_action(acts[1]["id"])
    assert plan.seats(store) == []
    with pytest.raises(ValueError, match="gone|already"):
        store.undo_ai_action(acts[1]["id"])


def anyio_tools(server):
    import anyio

    async def go():
        return await server.list_tools()
    return anyio.run(go)


def test_undo_is_refused_when_the_seat_changed_since(store):
    server = build_server(store)
    sid = structured(call(server, "set_plan_seat", role="Seat"))["seat_id"]
    plan.save(store, sid, role="Renamed")
    act = store.ai_actions()[0]
    with pytest.raises(ValueError, match="changed again"):
        store.undo_ai_action(act["id"])


def test_cautious_mode_holds_a_seat_write(store):
    server = build_server(store)
    review.set_cautious(store, True)
    res = call(server, "set_plan_seat", role="Seat", target_start="2027-01-15")
    assert "Cautious mode" in text(res) and plan.seats(store) == []
    r = review.pending(store)[0]
    assert r["kind"] == "plan" and review.field_lines(r)
    review.keep(store, r["id"])
    assert [s["role"] for s in plan.seats(store)] == ["Seat"]


def test_plan_tool_errors_are_plain(store):
    server = build_server(store)
    with pytest.raises(Exception, match="needs a role"):
        call(server, "set_plan_seat", target_start="2027-01-01")
    with pytest.raises(Exception, match="open \\(hiring\\), filled or paused"):
        call(server, "set_plan_seat", role="X", status="maybe")
    with pytest.raises(Exception, match="Couldn't read"):
        call(server, "set_plan_seat", role="X", target_start="whenever")


def test_plan_page_forms_and_wording(store):
    job = _seat_job(store, n_screening=1)
    c = local_client(store)
    page = c.get("/plan")
    assert page.status_code == 200 and "No seats yet" in page.text and "—" not in page.text and "–" not in page.text
    r = c.post("/plan/seat", data={"role": "Founding backend", "job_id": str(job["id"]),
                                    "target_start": _iso(_today() + timedelta(days=25))},
               follow_redirects=False)
    assert "Added the seat" in flash_of(r)
    html = c.get("/plan").text
    assert "Founding backend" in html and "Behind" in html and "What to do this week" in html
    assert "Plan my week with Claude" in html and "—" not in html and "–" not in html
    sid = plan.seats(store)[0]["id"]
    r = c.post("/plan/seat", data={"seat_id": str(sid), "role": "Founding backend", "job_id": "",
                                    "target_start": "", "status": "open"}, follow_redirects=False)
    assert "Saved the seat" in flash_of(r) and plan.get(store, sid)["job_id"] is None
    r = c.post("/plan/settings", data={"notice_days": "30"}, follow_redirects=False)
    assert plan.notice_days(store) == 30 and plan.lead_days(store)["offer"] == 37
    assert "Notice must be" in flash_of(c.post("/plan/settings", data={"notice_days": "x"}, follow_redirects=False), "flash_error")
    r = c.post(f"/plan/seat/{sid}/remove", follow_redirects=False)
    assert "Took the seat" in flash_of(r) and plan.seats(store) == []


def test_changes_page_lists_the_seat_write(store):
    server = build_server(store)
    call(server, "set_plan_seat", role="Seat")
    html = local_client(store).get("/changes?who=ai").text
    assert "Added the seat" in html


def test_time_zone_banner_has_a_one_click_use_button_and_offer_terms_are_private(store, monkeypatch):
    monkeypatch.setattr(reports, "computer_on_utc", lambda: True)  # the bar only shows on a computer set to UTC
    c = local_client(store)
    assert "data-tz-use" in c.get("/").text
    job, m = _offer_setup(store)
    actions.set_inhouse(store, True)
    page = c.get(f"/jobs/{job['id']}/offers/{m['id']}").text
    assert "Private to you. They never go in a status update." in page
