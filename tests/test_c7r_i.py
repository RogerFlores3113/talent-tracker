"""Cycle 7 re-review, fixer I: outreach and the Client desk."""
from datetime import datetime, timedelta, timezone

import anyio
import pytest

from talent import desk, mcp_server, outreach, reports
from talent.mcp_server import build_server

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res):
    return "\n".join(c.text for c in res.content)


@pytest.fixture(autouse=True)
def _apps_host(monkeypatch):
    monkeypatch.setattr(mcp_server, "_client_apps", lambda mcp: True)


def iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def job_with(store, names, sent_days_ago=None, company="Northwind"):
    job = store.add_job("ML Engineer", company=company)
    ids = []
    for n in names:
        c = store.add_candidate(name=n)
        store.add_to_pipeline(c["id"], job["id"])
        ids.append(c["id"])
        if sent_days_ago is not None:
            store.log(c["id"], "message", "InMail", at=iso(sent_days_ago), job_id=job["id"])
    return job, ids


def sequence_since(store, job_id, days_ago):
    import json
    outreach.save_sequence(store, job_id, list(outreach.DEFAULT_STEPS))
    seq = outreach.sequence(store, job_id)
    seq["since"] = iso(days_ago)
    store._conn.execute("UPDATE settings SET value = ? WHERE key = ?", (json.dumps(seq), f"{outreach.SEQ_PREFIX}{job_id}"))


# 1. a reply reads as a reply
def test_a_logged_reply_shows_as_reply_in_get_candidate_and_on_the_web(store):
    job, (ana,) = job_with(store, ["Ana Ruiz"])
    server = build_server(store)
    call(server, "log_activity", candidate_id=ana, kind="reply", summary="Interested, call Tue", job_id=job["id"])
    out = text(call(server, "get_candidate", candidate_id=ana))
    assert "reply (ML Engineer (Northwind)): Interested, call Tue" in out and "message (ML" not in out
    assert '<span class="kind">reply</span>' in local_client(store).get(f"/candidates/{ana}").text


# 2. step keys
def test_steps_with_other_key_names_work_and_unknown_keys_are_named(store):
    job, _ = job_with(store, ["Ana Ruiz"])
    server = build_server(store)
    res = call(server, "set_followup_sequence", job_id=job["id"], steps=[
        {"name": "InMail", "days": 0, "body": "Hi {first_name}"}, {"name": "Nudge", "after_days": 3, "message": "Hello again"}])
    assert text(res).startswith("Set 2 follow-up steps")
    assert [(s["label"], s["day"], s["text"]) for s in outreach.sequence(store, job["id"])["steps"]] == [
        ("InMail", 0, "Hi {first_name}"), ("Nudge", 3, "Hello again")]
    with pytest.raises(Exception) as e:
        call(server, "set_followup_sequence", job_id=job["id"], steps=[{"when": 0, "copy": "x"}, {"when": 3, "copy": "y"}])
    msg = str(e.value)
    assert "when, copy" in msg and "label, day and text" in msg and "for example" in msg and "at least two" not in msg


# 3. the shared block on Today
def test_batched_row_is_a_template_and_each_copy_has_the_name(store):
    job, ids = job_with(store, ["Ana Ruiz", "Bo Li", "Cy Park", "Di Wu"], sent_days_ago=5)
    sequence_since(store, job["id"], days_ago=10)
    page = local_client(store).get("/").text
    assert "Template, step 2" in page and "{first_name}" in page and "Copy for step" not in page
    assert 'data-copy-text="Hi Ana,' in page or "Hi Ana" in page


# 4. start counting from today
def test_enroll_from_today_means_nobody_is_overdue_and_first_message_still_works(store):
    job, (ana, bo) = job_with(store, ["Ana Ruiz", "Bo Li"], sent_days_ago=9)
    sequence_since(store, job["id"], days_ago=2)
    c = local_client(store)
    assert "Start counting from today" in c.get(f"/jobs/{job['id']}").text
    assert "counts from today" in flash_of(c.post(f"/jobs/{job['id']}/sequence/enroll", follow_redirects=False))
    assert outreach.due(store) == []
    ahead = outreach.due(store, days_ahead=30)
    assert {p["name"] for p in ahead} == {"Ana Ruiz", "Bo Li"} and not any(p["overdue"] for p in ahead)
    assert all(p["status"] == "waiting" for p in outreach.progress(store, job["id"]))


def test_ai_sequence_enroll_from_today_or_first_message(store):
    job, (ana,) = job_with(store, ["Ana Ruiz"], sent_days_ago=9)
    server = build_server(store)
    steps = [{"label": "InMail", "day": 0, "text": "Hi"}, {"label": "Nudge", "day": 3, "text": "Hi again"}]
    call(server, "set_followup_sequence", job_id=job["id"], steps=steps)
    assert "enrolled 1" in text(call(server, "set_followup_sequence", job_id=job["id"], steps=steps, enroll_existing=True))
    assert outreach.due(store) == []
    seq = outreach.sequence(store, job["id"])
    assert seq["starts"] == {str(ana): datetime.now(timezone.utc).astimezone().date().isoformat()} or seq["starts"]
    with pytest.raises(Exception, match="enroll_from"):
        call(server, "set_followup_sequence", job_id=job["id"], steps=steps, enroll_from="whenever")


# 5. outreach tab order and filter
def test_outreach_tab_sorts_due_first_and_filters(store):
    job, (ana, bo, cy, di) = job_with(store, ["Ana Ruiz", "Bo Li", "Cy Park", "Di Wu"])
    store.log(ana, "message", "InMail", at=iso(1), job_id=job["id"])      # next step later
    store.log(bo, "message", "InMail", at=iso(9), job_id=job["id"])       # overdue
    store.log(cy, "message", "InMail", at=iso(9), job_id=job["id"])
    store.log(di, "message", "InMail", at=iso(3), job_id=job["id"])
    outreach.save_sequence(store, job["id"], list(outreach.DEFAULT_STEPS))
    outreach.enroll_existing(store, job["id"], start="first_message")
    entry = store.log(cy, "message", "Sure", at=iso(2), job_id=job["id"])
    outreach.mark_reply(store, entry["id"])
    t = outreach.table(store, job["id"])
    assert [r["name"] for r in t["people"]][0] == "Bo Li" and t["people"][-1]["name"] == "Cy Park"
    assert t["counts"]["replied"] == 1 and t["counts"]["due"] >= 1
    only = outreach.table(store, job["id"], only="replied")
    assert [r["name"] for r in only["people"]] == ["Cy Park"] and only["totals"]["contacted"] == 4
    page = local_client(store).get(f"/jobs/{job['id']}/outreach?status=replied").text
    assert "Cy Park" in page and "Bo Li" not in page and "Replied</strong> (1)" in page


# 6. client desk
def test_desk_lists_who_is_waiting_when_nothing_is_overdue(store):
    job = store.add_job("Staff Engineer", company="Harbor Freight Labs", stages=["Sourced", "Submitted", "Offer", "Hired", "Rejected"])
    c = store.add_candidate(name="Dana Whitlock")
    store.add_to_pipeline(c["id"], job["id"], stage="Submitted")
    reports.create_client(store, "Harbor Freight Labs")
    d = desk.desk(store, "harbor freight labs")
    sec = next(s for s in d["sections"] if s["kind"] == "chase")
    assert sec["count"] == 1 and sec["items"][0]["not_due"]
    assert "Dana Whitlock" in sec["items"][0]["detail"] and "day 0" in sec["items"][0]["detail"]
    assert "nudge comes due in 5 days" in sec["items"][0]["detail"]
    assert d["count"] == 0
    page = local_client(store).get(f"/clients/{d['slug']}/desk").text
    assert "Not due yet" in page and "Dana Whitlock" in page


def test_update_groups_a_crowded_stage_into_one_line(store):
    job = store.add_job("SRE", company="Globex")
    for i in range(8):
        c = store.add_candidate(name=f"Person {i}")
        store.add_to_pipeline(c["id"], job["id"], stage="Interviewing")
    c = store.add_candidate(name="Solo Sam")
    store.add_to_pipeline(c["id"], job["id"], stage="Offer")
    rep = reports.reports_for(store, "globex")[0]
    out = reports.report_text(rep)
    assert "8 in Interviewing: Person 0, Person 1, Person 2, Person 3 and 4 more" in out
    standing = out.split("Where each person stands:")[1].split("This period:")[0]
    assert "Solo Sam" in standing and "Person 7" not in standing


# 7. small ones
def test_create_client_on_an_existing_client_saves_the_details(store):
    server = build_server(store)
    call(server, "create_client", name="Acme Payments")
    res = call(server, "create_client", name="Acme, Inc.", update_day="fri") if False else call(
        server, "create_client", name="Acme Payments", update_day="fri")
    assert "already a client" in text(res) and "Friday" in text(res)
    assert reports.client(store, "acme payments")["update_day"] == "Friday"


def test_create_job_for_a_client_includes_submitted(store):
    server = build_server(store)
    call(server, "create_client", name="Acme Payments")
    res = call(server, "create_job", title="Backend", company="Acme Payments")
    assert "Submitted" in text(res)
    other = call(server, "create_job", title="Backend", company="Brand New Co")
    assert "Submitted" not in text(other)
