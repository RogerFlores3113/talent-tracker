"""Cycle 6, fixer E: the offer and close workspace (offers.py)."""
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from talent import actions, offers, reports, review
from talent.mcp_server import build_server
from talent.store import AI_NOTE_CAP, AI_OVERWRITE_CAP, _MIGRATIONS, as_local

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res):
    return "\n".join(c.text for c in res.content)


def _setup(store, stage="Offer", company="Acme"):
    job = store.add_job("Founding Backend Engineer", company=company)
    marcus = store.add_candidate(name="Marcus Oyelaran")
    store.add_to_pipeline(marcus["id"], job["id"])
    store.move_stage(marcus["id"], job["id"], stage, by="app")
    return job, marcus


def _today():
    return as_local(datetime.now(timezone.utc)).date()


def _offer_items(store):
    return [i for i in actions.action_items(store)["items"] if i["kind"] == "offer"]


# ------------------------------------------------------------------ the record
def test_offers_table_is_the_last_migration(store):
    assert store.schema_version == len(_MIGRATIONS) == 11
    assert store._row("SELECT count(*) AS n FROM offers")["n"] == 0


def test_save_creates_then_updates_one_offer_per_person_and_job(store):
    job, m = _setup(store)
    res = offers.save(store, m["id"], job["id"], salary=" $185,000  base ", answer_by="2026-12-01")
    assert res["created"] and res["offer"]["status"] == "drafted"
    assert res["offer"]["salary"] == "$185,000 base" and res["offer"]["answer_by"] == "2026-12-01"
    res = offers.save(store, m["id"], job["id"], status="out", equity="0.4%")
    assert not res["created"] and set(res["changed"]) == {"status", "equity"}
    o = offers.get(store, m["id"], job["id"])
    assert o["status"] == "out" and o["out_at"] and o["salary"] == "$185,000 base"
    assert store._row("SELECT count(*) AS n FROM offers")["n"] == 1


def test_offer_needs_the_person_in_the_job(store):
    job = store.add_job("Backend")
    ana = store.add_candidate(name="Ana Ruiz")
    with pytest.raises(ValueError, match="isn't in the pipeline"):
        offers.save(store, ana["id"], job["id"], salary="$1")


def test_bad_values_are_refused(store):
    job, m = _setup(store)
    with pytest.raises(ValueError, match="answer-by date"):
        offers.save(store, m["id"], job["id"], answer_by="whenever")
    with pytest.raises(ValueError, match="too long"):
        offers.save(store, m["id"], job["id"], salary="x" * 201)
    with pytest.raises(ValueError, match="Status must be"):
        offers.record_answer(store, m["id"], job["id"], "maybe")


def test_status_line_says_how_long_it_has_been_out_and_when_the_answer_is_due(store):
    job, m = _setup(store)
    today = _today()
    offers.save(store, m["id"], job["id"], status="out", answer_by=(today + timedelta(days=3)).isoformat())
    three_days = datetime.now(timezone.utc) + timedelta(days=3)
    o = offers.get(store, m["id"], job["id"])
    assert offers.status_line(o).startswith("Offer out today, answer due ")
    assert offers.status_line(o, three_days).startswith("Offer out 3 days, answer due today")
    assert "answer was due" in offers.status_line(o, three_days + timedelta(days=1))
    assert offers.status_line({"status": "drafted"}) == "Offer drafted, not out yet"


# ------------------------------------------------------------------ today
def test_today_asks_for_an_offer_record_in_the_offer_stage(store):
    job, m = _setup(store)
    items = _offer_items(store)
    assert len(items) == 1 and "with no offer recorded" in items[0]["title"]
    assert items[0]["href"] == f"/jobs/{job['id']}/offers/{m['id']}"
    offers.save(store, m["id"], job["id"])
    assert not _offer_items(store)


def test_today_lists_an_answer_due_tomorrow_or_overdue_but_not_later(store):
    job, m = _setup(store)
    today = _today()
    offers.save(store, m["id"], job["id"], status="out", answer_by=(today + timedelta(days=5)).isoformat())
    assert not _offer_items(store)
    offers.save(store, m["id"], job["id"], answer_by=(today + timedelta(days=1)).isoformat())
    (item,) = _offer_items(store)
    assert item["title"].endswith("answer due tomorrow") and not item["overdue"]
    offers.save(store, m["id"], job["id"], answer_by=(today - timedelta(days=2)).isoformat())
    (item,) = _offer_items(store)
    assert "answer was due" in item["title"] and item["overdue"]
    page = local_client(store).get("/").text
    assert "Offer to Marcus Oyelaran" in page and "Open offer" in page


def test_accepted_offers_the_move_to_hired_on_today_and_it_is_undoable(store):
    job, m = _setup(store)
    offers.save(store, m["id"], job["id"], status="out")
    res = offers.record_answer(store, m["id"], job["id"], "accepted")
    assert res["next"] == {"kind": "hired", "to": "Hired", "notes": None}
    (item,) = _offer_items(store)
    assert item["move_to"] == "Hired" and "accepted the offer" in item["title"]
    c = local_client(store)
    assert "Move to Hired" in c.get("/").text
    r = c.post(item["move_url"], data={"to": "hired", "next": "/"}, follow_redirects=False)
    assert "Moved Marcus Oyelaran to Hired" in flash_of(r)
    assert store.candidate_pipelines(m["id"])[0]["stage"] == "Hired"
    assert not _offer_items(store)  # done: nothing left to ask
    entry = store._row("SELECT id FROM timeline WHERE candidate_id = ? AND kind = 'stage' ORDER BY id DESC LIMIT 1",
                       (m["id"],))
    store.undo_change(entry["id"])
    assert store.candidate_pipelines(m["id"])[0]["stage"] == "Offer"


def test_declined_moves_out_with_the_reason(store):
    job, m = _setup(store)
    c = local_client(store)
    base = f"/jobs/{job['id']}/offers/{m['id']}"
    c.post(base, data={"status": "out", "salary": "$150,000"})
    r = c.post(base + "/answer", data={"answer": "declined", "reason": "Took another offer"}, follow_redirects=False)
    assert "Recorded: offer declined" in flash_of(r) and "Withdrawn" in flash_of(r)
    page = c.get(base).text
    assert "Move Marcus Oyelaran to Withdrawn" in page and "Declined the offer: Took another offer" in page
    c.post(base + "/move", data={"to": "out"})
    app = store.candidate_pipelines(m["id"])[0]
    assert app["stage"] == "Withdrawn" and app["notes"] == "Reason: Declined the offer: Took another offer"
    assert "Move Marcus" not in c.get(base).text


def test_withdrawn_uses_the_rejection_stage(store):
    job, m = _setup(store)
    res = offers.record_answer(store, m["id"], job["id"], "withdrawn", "Budget cut")
    assert res["next"]["to"] == "Rejected" and res["next"]["notes"] == "Reason: Offer withdrawn: Budget cut"


# ------------------------------------------------------------------ pages
def test_offer_page_saves_and_profile_board_and_decide_show_it(store):
    job, m = _setup(store)
    c = local_client(store)
    base = f"/jobs/{job['id']}/offers/{m['id']}"
    profile = c.get(f"/candidates/{m['id']}").text
    assert f'href="{base}">Make an offer' in profile
    assert "No offer recorded yet" in c.get(base).text
    due = (_today() + timedelta(days=3)).isoformat()
    r = c.post(base, data={"status": "out", "salary": "$185,000", "notes": "Wants a hybrid week", "answer_by": due},
               follow_redirects=False)
    assert flash_of(r) == "Saved the offer."
    assert "Offer out today, answer due" in c.get(f"/candidates/{m['id']}").text
    assert "Offer out today, answer due" in c.get(f"/jobs/{job['id']}?view=board").text
    store.log(m["id"], "interview", "Founder call", at="2026-11-01T10:00:00+00:00", job_id=job["id"])
    assert "Offer out today" in c.get(f"/jobs/{job['id']}/decide").text
    page = c.get(base).text
    assert "Offer summary to paste" in page and "$185,000" in page


def test_decide_offers_make_an_offer_for_each_finalist(store):
    job = store.add_job("Backend")
    ana = store.add_candidate(name="Ana Ruiz")
    store.add_to_pipeline(ana["id"], job["id"], "Interviewing")
    store.log(ana["id"], "interview", "Onsite", at="2026-11-01T10:00:00+00:00", job_id=job["id"])
    page = local_client(store).get(f"/jobs/{job['id']}/decide").text
    assert f'href="/jobs/{job["id"]}/offers/{ana["id"]}">Make an offer' in page


def test_offer_page_404s_for_someone_not_in_the_job(store):
    job = store.add_job("Backend")
    ana = store.add_candidate(name="Ana Ruiz")
    assert local_client(store).get(f"/jobs/{job['id']}/offers/{ana['id']}").status_code == 404


def test_summary_is_paste_ready_and_leaves_the_call_notes_out(store):
    job, m = _setup(store)
    offers.save(store, m["id"], job["id"], salary="$185,000 base", equity="0.4%", start_date="2026-12-07",
                answer_by="2026-11-13", notes="Partner is relocating, keep it quiet")
    text_ = offers.summary_text(store, offers.get(store, m["id"], job["id"]))
    assert text_.startswith("Hi Marcus,") and "Salary: $185,000 base" in text_ and "Equity: 0.4%" in text_
    assert "Start date: Monday, December 7, 2026" in text_ and "answer by Friday, November 13" in text_
    assert "Partner" not in text_


def test_offer_pages_have_no_dashes(store):
    job, m = _setup(store)
    c = local_client(store)
    base = f"/jobs/{job['id']}/offers/{m['id']}"
    pages = [c.get(base).text]
    c.post(base, data={"status": "out", "answer_by": "2026-11-01"})
    offers.record_answer(store, m["id"], job["id"], "declined", "Compensation")
    pages += [c.get(base).text, c.get("/").text, c.get(f"/candidates/{m['id']}").text]
    for t in pages:
        assert "—" not in t and " – " not in t


# ------------------------------------------------------------------ privacy
def test_terms_never_reach_the_paste_text_or_client_update(store):
    job, m = _setup(store)
    offers.save(store, m["id"], job["id"], status="out", salary="$185,000", bonus="$20,000", answer_by="2026-11-13",
                notes="secret")
    paste, update = actions.job_status_text(store, job["id"]), reports.report_text(reports.report(store, "acme"))
    for t in (paste, update):
        assert "185" not in t and "Offers:" not in t and "secret" not in t
    offers.save(store, m["id"], job["id"], share_status=True)
    paste, update = actions.job_status_text(store, job["id"]), reports.report_text(reports.report(store, "acme"))
    assert "Offers:\n- Marcus Oyelaran: offer out, answer due Fri Nov 13" in paste
    assert "- Marcus Oyelaran (Founding Backend Engineer): offer out, answer due Fri Nov 13" in update
    for t in (paste, update):
        assert "185" not in t and "20,000" not in t and "secret" not in t


def test_web_share_tick_turns_on_and_off(store):
    job, m = _setup(store)
    c = local_client(store)
    base = f"/jobs/{job['id']}/offers/{m['id']}"
    c.post(base, data={"status": "out", "share_status": "1"})
    assert offers.get(store, m["id"], job["id"])["share_status"] == 1
    c.post(base, data={"status": "out"})
    assert offers.get(store, m["id"], job["id"])["share_status"] == 0


def test_mcp_job_pipeline_keeps_offer_status_out_of_the_paste_block(store):
    job, m = _setup(store)
    offers.save(store, m["id"], job["id"], status="out", salary="$185,000")
    res = call(build_server(store), "job_pipeline", job_id=job["id"])
    assert "Offer out" not in res.content[0].text and "185" not in text(res)
    assert any("offers: Marcus Oyelaran: Offer out today" in c.text for c in res.content[1:])


# ------------------------------------------------------------------ MCP tools
def test_record_offer_logs_and_undo_takes_it_back(store):
    job, m = _setup(store)
    server = build_server(store)
    res = call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], salary="$185,000", status="out",
               answer_by="2026-11-13")
    assert text(res).startswith('Recorded the offer for "Marcus Oyelaran" on Founding Backend Engineer (Acme)')
    assert "{" not in text(res) and res.structuredContent["offer"]["salary"] == "$185,000"
    (a,) = store.ai_actions()
    assert a["kind"] == "offer_terms" and a["undoable"] and "185" not in a["summary"]
    call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], salary="$190,000")
    second = store.ai_actions()[0]
    assert second["summary"].startswith("Changed the offer for") and not store.ai_action(a["id"])["undoable"]
    store.undo_ai_action(second["id"])
    assert offers.get(store, m["id"], job["id"])["salary"] == "$185,000"
    store.undo_ai_action(a["id"])
    assert offers.get(store, m["id"], job["id"]) is None


def test_undo_is_refused_once_the_recruiter_changed_the_offer(store):
    job, m = _setup(store)
    call(build_server(store), "record_offer", candidate_id=m["id"], job_id=job["id"], salary="$185,000")
    offers.save(store, m["id"], job["id"], salary="$200,000")
    a = store.ai_actions()[0]
    assert a["blocked"] == "the offer was changed again since"
    with pytest.raises(ValueError, match="changed again since"):
        store.undo_ai_action(a["id"])


def test_record_offer_answer_logs_and_undoes(store):
    job, m = _setup(store)
    offers.save(store, m["id"], job["id"], status="out")
    server = build_server(store)
    res = call(server, "record_offer_answer", candidate_id=m["id"], job_id=job["id"], answer="declined",
               reason="Took another offer")
    assert 'Recorded that "Marcus Oyelaran" declined the offer' in text(res)
    assert res.structuredContent["next_move"]["stage"] == "Withdrawn"
    assert store.candidate_pipelines(m["id"])[0]["stage"] == "Offer"  # the AI app doesn't move them by itself
    a = store.ai_actions()[0]
    assert a["kind"] == "offer_answer" and "declined the offer" in a["summary"]
    again = call(server, "record_offer_answer", candidate_id=m["id"], job_id=job["id"], answer="declined",
                 reason="Took another offer")
    assert text(again).startswith("Nothing changed")
    store.undo_ai_action(a["id"])
    o = offers.get(store, m["id"], job["id"])
    assert o["status"] == "out" and o["reason"] is None


def test_record_offer_refusals(store):
    job, m = _setup(store)
    server = build_server(store)
    with pytest.raises(ToolError, match="enum|accepted"):
        call(server, "record_offer_answer", candidate_id=m["id"], job_id=job["id"], answer="maybe")
    call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], salary="$1", equity="1%")
    with pytest.raises(ToolError, match="Clear one field per call"):
        call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], salary="", equity="")
    other = store.add_candidate(name="Ana Ruiz")
    with pytest.raises(ToolError, match="isn't in the pipeline"):
        call(server, "record_offer", candidate_id=other["id"], job_id=job["id"], salary="$1")
    assert not offers.get(store, other["id"], job["id"])


def test_ai_cannot_tick_share_status(store):
    job, m = _setup(store)
    res = offers.save(store, m["id"], job["id"], by="ai", share_status=True, salary="$1")
    assert res["offer"]["share_status"] == 0


def test_list_offers_reads_them_back(store):
    job, m = _setup(store)
    server = build_server(store)
    assert text(call(server, "list_offers")) == "No offers recorded yet."
    offers.save(store, m["id"], job["id"], status="out", salary="$185,000")
    out = text(call(server, "list_offers", job_id=job["id"]))
    assert '"Marcus Oyelaran"' in out and "Offer out today" in out and "salary $185,000" in out


# ------------------------------------------------------------------ Cautious mode and caps
def test_cautious_mode_holds_both_tools_and_keep_applies_once(store):
    job, m = _setup(store)
    review.set_cautious(store, True)
    server = build_server(store)
    res = call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], salary="$185,000", status="out")
    assert text(res).startswith("Waiting for the recruiter's review") and offers.get(store, m["id"], job["id"]) is None
    rid = res.structuredContent["pending_review"]["id"]
    page = local_client(store).get("/review").text
    assert "Offer change" in page and "would be $185,000" in page
    assert "1 change to this offer" in local_client(store).get(f"/jobs/{job['id']}/offers/{m['id']}").text
    review.keep(store, rid)
    o = offers.get(store, m["id"], job["id"])
    assert o["salary"] == "$185,000" and o["status"] == "out"
    with pytest.raises(ValueError):
        review.keep(store, rid)
    assert store.ai_actions()[0]["kind"] == "offer_terms"  # kept: logged, and undoable on Changes
    res = call(server, "record_offer_answer", candidate_id=m["id"], job_id=job["id"], answer="accepted")
    rid = res.structuredContent["pending_review"]["id"]
    assert offers.get(store, m["id"], job["id"])["status"] == "out"
    review.keep(store, rid)
    assert offers.get(store, m["id"], job["id"])["status"] == "accepted"


def test_cautious_keep_is_refused_when_the_offer_changed_since(store):
    job, m = _setup(store)
    offers.save(store, m["id"], job["id"], salary="$150,000")
    review.set_cautious(store, True)
    res = call(build_server(store), "record_offer", candidate_id=m["id"], job_id=job["id"], salary="$185,000")
    offers.save(store, m["id"], job["id"], salary="$160,000")
    with pytest.raises(ValueError, match="changed since"):
        review.keep(store, res.structuredContent["pending_review"]["id"])
    assert offers.get(store, m["id"], job["id"])["salary"] == "$160,000"


def test_per_person_cap_on_ai_offer_writes(store):
    job, m = _setup(store)
    server = build_server(store)
    for i in range(AI_NOTE_CAP):
        call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], notes=f"call {i}")
    with pytest.raises(ToolError, match="already changed the offer"):
        call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], notes="one more")


def test_offer_overwrites_count_toward_the_hourly_cap(store):
    job, m = _setup(store)
    server = build_server(store)
    call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], salary="$1", equity="1%")
    assert store.ai_overwrites_this_hour()["count"] == 0  # filling empty values
    call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], salary="$2", equity="2%")
    call(server, "record_offer_answer", candidate_id=m["id"], job_id=job["id"], answer="accepted")
    assert store.ai_overwrites_this_hour()["count"] == 3
    with store._write():  # other AI overwrites this hour fill the cap
        for _ in range(AI_OVERWRITE_CAP - 3):
            store._log_ai("interview_cancel", old={}, new={})
    with pytest.raises(ToolError, match="already replaced or cleared"):
        call(server, "record_offer_answer", candidate_id=m["id"], job_id=job["id"], answer="declined")


def test_undo_all_ai_takes_back_offer_writes(store):
    job, m = _setup(store)
    server = build_server(store)
    call(server, "record_offer", candidate_id=m["id"], job_id=job["id"], salary="$185,000")
    call(server, "record_offer_answer", candidate_id=m["id"], job_id=job["id"], answer="accepted")
    c = local_client(store)
    assert "Recorded that" in c.get("/changes?who=ai").text
    c.post("/changes/undo-ai", data={"window": "24"})
    assert offers.get(store, m["id"], job["id"]) is None


def test_offer_goes_with_the_person(store):
    job, m = _setup(store)
    offers.save(store, m["id"], job["id"], salary="$185,000")
    store.delete_candidate(m["id"])
    assert store._row("SELECT count(*) AS n FROM offers")["n"] == 0


def test_merge_keeps_the_offer(store):
    job, m = _setup(store)
    twin = store.add_candidate(name="Marcus O.", email="m@x.com")
    offers.save(store, m["id"], job["id"], salary="$185,000")
    store.merge_candidates(twin["id"], m["id"])
    assert offers.get(store, twin["id"], job["id"])["salary"] == "$185,000"
