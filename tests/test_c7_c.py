"""Cycle 7, fixer C: outreach. Enroll people already messaged, batch follow-ups on Today, set_followup_sequence,
the Outreach tab and list_outreach, and small fixes."""
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from talent import actions, mcp_server, outreach, review
from talent.mcp_server import build_server

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res):
    return "\n".join(c.text for c in res.content)


def iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def job_with(store, names, sent_days_ago=None, source=None):
    job = store.add_job("ML Engineer", company="Northwind")
    ids = []
    for n in names:
        c = store.add_candidate(name=n, source=source) if source else store.add_candidate(name=n)
        store.add_to_pipeline(c["id"], job["id"])
        ids.append(c["id"])
        if sent_days_ago is not None:
            store.log(c["id"], "message", "InMail", at=iso(sent_days_ago), job_id=job["id"])
    return job, ids


def sequence_since(store, job_id, days_ago):
    """Save the default steps with the sequence dated days_ago (what a sequence set earlier looks like)."""
    import json
    outreach.save_sequence(store, job_id, list(outreach.DEFAULT_STEPS))
    seq = outreach.sequence(store, job_id)
    seq["since"] = iso(days_ago)
    store._conn.execute("UPDATE settings SET value = ? WHERE key = ?", (json.dumps(seq), f"{outreach.SEQ_PREFIX}{job_id}"))


# --------------------------------------------------------------- 1. enroll
def test_people_already_messaged_are_listed_and_can_be_enrolled(store):
    job, (ana, bo) = job_with(store, ["Ana Ruiz", "Bo Li"], sent_days_ago=9)
    sequence_since(store, job["id"], days_ago=2)
    assert outreach.progress(store, job["id"]) == []
    assert [p["name"] for p in outreach.not_following(store, job["id"])] == ["Ana Ruiz", "Bo Li"]
    c = local_client(store)
    page = c.get(f"/jobs/{job['id']}").text
    assert "2 people you already messaged aren't following" in page and "Enroll people already in Contacted" in page
    assert "last message" in page
    r = c.post(f"/jobs/{job['id']}/sequence/enroll", data={"start": "first_message"}, follow_redirects=False)
    assert "Enrolled 2 people" in flash_of(r)
    assert outreach.not_following(store, job["id"]) == []
    due = outreach.due(store)
    assert {p["name"] for p in due} == {"Ana Ruiz", "Bo Li"} and due[0]["step"] == 2 and due[0]["overdue"]
    # Editing the steps keeps who is enrolled.
    outreach.save_sequence(store, job["id"], list(outreach.DEFAULT_STEPS[:2]))
    assert len(outreach.due(store)) == 2
    assert "Nobody needed enrolling" in flash_of(c.post(f"/jobs/{job['id']}/sequence/enroll", follow_redirects=False))


def test_enrolling_needs_a_sequence_and_skips_replied_and_moved_on(store):
    job, (ana, bo, cy) = job_with(store, ["Ana Ruiz", "Bo Li", "Cy Park"], sent_days_ago=9)
    with pytest.raises(ValueError, match="Set up follow-ups"):
        outreach.enroll_existing(store, job["id"])
    sequence_since(store, job["id"], days_ago=2)
    entry = store.log(bo, "message", "Sure!", at=iso(5), job_id=job["id"])
    outreach.mark_reply(store, entry["id"])
    store.move_stage(cy, job["id"], "Screening", by="app")
    assert [p["name"] for p in outreach.not_following(store, job["id"])] == ["Ana Ruiz"]
    r = local_client(store).post(f"/jobs/{job['id']}/sequence/enroll", follow_redirects=False)
    assert "Enrolled 1 person (Ana Ruiz)" in flash_of(r)


# --------------------------------------------------------- 2. batch on Today
def test_follow_ups_due_at_one_step_fold_into_one_row_with_one_sent_all_button(store):
    job, ids = job_with(store, ["Ana Ruiz", "Bo Li", "Cy Park", "Di Wu"], sent_days_ago=5)
    sequence_since(store, job["id"], days_ago=10)
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "followup"]
    assert len(items) == 1 and items[0]["title"].startswith("4 follow-ups due: Follow-up for ML Engineer")
    assert [b["name"] for b in items[0]["batch"]] == ["Ana Ruiz", "Bo Li", "Cy Park", "Di Wu"]
    assert "{first_name}" in items[0]["draft"] and "ML Engineer role at Northwind" in items[0]["draft"]
    c = local_client(store)
    page = c.get("/").text
    assert "I sent them all" in page and page.count('name="candidate_ids"') == 4 and "Copy for Ana" in page
    r = c.post(f"/jobs/{job['id']}/followups/batch", data={"candidate_ids": [str(i) for i in ids]}, follow_redirects=False)
    assert "Logged the follow-up to 4 people" in flash_of(r)
    assert all(len([t for t in store.timeline(i) if t["kind"] == "message" and "Follow-up sent" in (t["body"] or "")]) == 1
               for i in ids)
    assert not [i for i in actions.action_items(store)["items"] if i["kind"] == "followup"]
    # Pressing it again logs nobody twice.
    r = c.post(f"/jobs/{job['id']}/followups/batch", data={"candidate_ids": [str(i) for i in ids]}, follow_redirects=False)
    assert "Nothing new to log" in flash_of(r) and "Already logged today" in flash_of(r)
    assert "Nobody was picked" in flash_of(c.post(f"/jobs/{job['id']}/followups/batch", data={}, follow_redirects=False),
                                           "flash_error")


def test_two_due_at_the_same_step_stay_separate_rows(store):
    job, ids = job_with(store, ["Ana Ruiz", "Bo Li"], sent_days_ago=5)
    sequence_since(store, job["id"], days_ago=10)
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "followup"]
    assert len(items) == 2 and not any(i.get("batch") for i in items)


# --------------------------------------------------- 3. set_followup_sequence
STEPS = [{"label": "InMail", "day": 0, "text": "Hi {first_name}, about {job}"},
         {"label": "Nudge", "day": 3, "text": "Hi {first_name}, following up"}]


def test_ai_sets_steps_and_enrolls_existing_and_it_can_be_undone(store):
    job, (ana,) = job_with(store, ["Ana Ruiz"], sent_days_ago=9)
    sequence_since(store, job["id"], days_ago=2)
    outreach.save_sequence(store, job["id"], list(outreach.DEFAULT_STEPS))  # a sequence is there already
    server = build_server(store)
    res = call(server, "set_followup_sequence", job_id=job["id"], steps=STEPS)
    assert text(res).startswith("Set 2 follow-up steps for ML Engineer (Northwind).")
    assert "replaced the steps" in text(res) and "1 people messaged earlier don't follow" in text(res)
    assert [s["label"] for s in outreach.sequence(store, job["id"])["steps"]] == ["InMail", "Nudge"]
    res = call(server, "set_followup_sequence", job_id=job["id"], steps=STEPS, enroll_existing=True,
               enroll_from="first_message")
    assert "enrolled 1 already messaged" in text(res) and res.structuredContent["enrolled"][0]["name"] == "Ana Ruiz"
    assert outreach.due(store)[0]["label"] == "Nudge"
    acts = [a for a in store.ai_actions() if a["kind"] == "sequence_set"]
    assert len(acts) == 2 and acts[0]["undoable"] and "Set the follow-up steps of ML Engineer (Northwind)" in acts[0]["summary"]
    assert "Set the follow-up steps" in local_client(store).get("/changes").text
    # The older one waits for the newer one; undoing the newest puts the earlier steps and the enrolled list back.
    store.undo_ai_action(acts[0]["id"])
    assert outreach.due(store) == [] and outreach.sequence(store, job["id"]).get("enrolled") is None
    store.undo_ai_action(acts[1]["id"])
    assert [s["label"] for s in outreach.sequence(store, job["id"])["steps"]] == ["First message", "Follow-up",
                                                                                  "Last follow-up"]


def test_set_followup_sequence_checks_steps_and_never_removes(store):
    job, _ = job_with(store, ["Ana Ruiz"])
    server = build_server(store)
    with pytest.raises(ToolError, match="No change made"):
        call(server, "set_followup_sequence", job_id=job["id"], steps=[])
    with pytest.raises(ToolError, match="later day"):
        call(server, "set_followup_sequence", job_id=job["id"], steps=[STEPS[0], {**STEPS[1], "day": 0}])
    with pytest.raises(ToolError, match="No job with id 99"):
        call(server, "set_followup_sequence", job_id=99, steps=STEPS)
    assert outreach.sequence(store, job["id"]) is None
    assert not [a for a in store.ai_actions() if a["kind"] == "sequence_set"]


def test_cautious_mode_holds_the_steps_until_kept(store):
    job, (ana,) = job_with(store, ["Ana Ruiz"], sent_days_ago=9)
    review.set_cautious(store, True)
    server = build_server(store)
    res = call(server, "set_followup_sequence", job_id=job["id"], steps=STEPS, enroll_existing=True)
    assert "Nothing changed yet" in text(res) and outreach.sequence(store, job["id"]) is None
    waiting = review.pending(store)
    assert len(waiting) == 1 and waiting[0]["kind"] == "sequence" and "enroll the people already messaged" in waiting[0]["summary"]
    assert "Set the follow-up steps of ML Engineer (Northwind)" in local_client(store).get("/review").text
    review.keep(store, waiting[0]["id"])
    seq = outreach.sequence(store, job["id"])
    assert len(seq["steps"]) == 2 and seq["enrolled"] == [ana]
    act = next(a for a in store.ai_actions() if a["kind"] == "sequence_set")
    assert act["confirmed_at"]


# ----------------------------------------------------------- 4. Outreach tab
def test_outreach_tab_lists_everyone_messaged_with_rates(store):
    job = store.add_job("ML Engineer", company="Northwind")
    people = {}
    for n, src in (("Ana Ruiz", "LinkedIn"), ("Bo Li", "LinkedIn"), ("Cy Park", "X-ray search"), ("Di Wu", "")):
        c = store.add_candidate(name=n, source=src)
        store.add_to_pipeline(c["id"], job["id"])
        people[n] = c["id"]
        store.log(c["id"], "message", "InMail", at=iso(10), job_id=job["id"])
    sequence_since(store, job["id"], days_ago=20)
    store.log(people["Ana Ruiz"], "message", "Nudge", at=iso(6), job_id=job["id"])
    reply = store.log(people["Ana Ruiz"], "message", "Yes!", at=iso(5), job_id=job["id"])
    outreach.mark_reply(store, reply["id"])
    reply = store.log(people["Cy Park"], "message", "Hello", at=iso(9), job_id=job["id"])
    outreach.mark_reply(store, reply["id"])
    t = outreach.table(store, job["id"])
    assert t["totals"] == {"contacted": 4, "replied": 2, "rate": "50%", "due": 3 - 1, "not_following": 0, "paused": 0}
    by_name = {r["name"]: r for r in t["people"]}
    assert by_name["Ana Ruiz"]["replied"] and by_name["Ana Ruiz"]["sent"] == 2
    assert by_name["Bo Li"]["status"] == "due" and by_name["Bo Li"]["step"] == "1 of 3"
    # Ana replied after her 2nd message, Cy after his 1st: 4 got step 1, 1 replied; 3 got step 2 (Ana, Bo? no).
    step1, step2 = t["by_step"][0], t["by_step"][1]
    assert (step1["sent"], step1["replied"]) == (4, 1) and (step2["sent"], step2["replied"]) == (1, 1)
    assert step2["rate"] == "100%"
    src = {s["source"]: s for s in t["by_source"]}
    assert src["LinkedIn"]["contacted"] == 2 and src["LinkedIn"]["replied"] == 1 and src["LinkedIn"]["rate"] == "50%"
    assert src["No source saved"]["contacted"] == 1
    c = local_client(store)
    page = c.get(f"/jobs/{job['id']}/outreach").text
    assert "4 messaged, 2 replied (50%)" in page and "Reply rate by step" in page and "X-ray search" in page
    assert "—" not in page and "Outreach</a>" in page
    assert c.get(f"/jobs/{job['id']}").text.count(f'/jobs/{job["id"]}/outreach"') >= 1


def test_outreach_tab_works_without_steps_and_for_an_empty_job(store):
    job, ids = job_with(store, ["Ana Ruiz"], sent_days_ago=3)
    empty = store.add_job("Designer")
    c = local_client(store)
    page = c.get(f"/jobs/{job['id']}/outreach").text
    assert "No follow-up steps are set" in page and "No steps set" in page
    assert "Nobody messaged yet" in c.get(f"/jobs/{empty['id']}/outreach").text
    assert c.get("/jobs/999/outreach").status_code == 404


def test_bulk_actions_log_sent_mark_replied_pause_and_enroll(store):
    job, (ana, bo, cy) = job_with(store, ["Ana Ruiz", "Bo Li", "Cy Park"], sent_days_ago=9)
    sequence_since(store, job["id"], days_ago=2)
    c = local_client(store)
    url = f"/jobs/{job['id']}/outreach/bulk"
    assert "Tick the people" in flash_of(c.post(url, data={"action": "sent"}, follow_redirects=False), "flash_error")
    r = c.post(url, data={"action": "enroll", "start": "first_message", "candidate_ids": [str(ana), str(bo), str(cy)]}, follow_redirects=False)
    assert "Enrolled 3 people" in flash_of(r)
    r = c.post(url, data={"action": "pause", "candidate_ids": [str(ana)]}, follow_redirects=False)
    assert "Paused follow-ups for 1 person" in flash_of(r)
    assert {p["name"] for p in outreach.due(store)} == {"Bo Li", "Cy Park"}
    assert "Paused" in c.get(f"/jobs/{job['id']}/outreach").text
    c.post(url, data={"action": "resume", "candidate_ids": [str(ana)]})
    assert len(outreach.due(store)) == 3
    r = c.post(url, data={"action": "sent", "candidate_ids": [str(bo)]}, follow_redirects=False)
    assert "Logged a message to 1 person (Bo Li)" in flash_of(r)
    assert "Already logged today" in flash_of(c.post(url, data={"action": "sent", "candidate_ids": [str(bo)]},
                                                     follow_redirects=False))
    r = c.post(url, data={"action": "replied", "candidate_ids": [str(cy)]}, follow_redirects=False)
    assert "Marked 1 as replied (Cy Park)" in flash_of(r)
    assert "all marked as replied" in flash_of(c.post(url, data={"action": "replied", "candidate_ids": [str(cy)]},
                                                      follow_redirects=False))
    assert {p["name"] for p in outreach.due(store)} == {"Ana Ruiz"}
    assert "Pick an action" in flash_of(c.post(url, data={"action": "nope", "candidate_ids": [str(cy)]},
                                               follow_redirects=False), "flash_error")


def test_list_outreach_has_the_same_data_and_is_capped(store):
    job, ids = job_with(store, ["Ana Ruiz", "Bo Li", "Cy Park"], sent_days_ago=9)
    sequence_since(store, job["id"], days_ago=2)
    server = build_server(store)
    res = call(server, "list_outreach", job_id=job["id"])
    assert res.structuredContent["totals"]["contacted"] == 3 and res.structuredContent["totals"]["not_following"] == 3
    assert text(res).splitlines()[0] == ("ML Engineer (Northwind): 3 messaged, 0 replied (0%), 0 follow-ups due, "
                                         "3 not following the steps.")
    assert res.structuredContent["by_step"][0]["sent"] == 3 and res.structuredContent["app_url"].endswith(f"/jobs/{job['id']}/outreach")
    assert '"Ana Ruiz"' in text(res) and "not_following" in text(res)
    with pytest.raises(ToolError, match="No job with id 99"):
        call(server, "list_outreach", job_id=99)
    for i in range(52):  # the list stops at 50 people; the totals cover everyone
        c = store.add_candidate(name=f"Person {i:02d}")
        store.add_to_pipeline(c["id"], job["id"])
        store.log(c["id"], "message", "InMail", at=iso(3), job_id=job["id"])
    res = call(server, "list_outreach", job_id=job["id"])
    assert len(res.structuredContent["people"]) == 50 and res.structuredContent["more"] == 5
    assert res.structuredContent["totals"]["contacted"] == 55 and "And 5 more" in text(res)


# ----------------------------------------------------------------- 5. small
def test_move_stage_result_has_only_this_calls_note(store):
    job, (ana,) = job_with(store, ["Ana Ruiz"])
    store.move_stage(ana, job["id"], "Contacted", "Kept from the Shortlist. Why they fit: strong", by="app")
    server = build_server(store)
    res = call(server, "move_stage", candidate_id=ana, job_id=job["id"], stage="Screening")
    assert "notes" not in res.structuredContent
    res = call(server, "move_stage", candidate_id=ana, job_id=job["id"], stage="Interviewing", notes="Great call")
    assert res.structuredContent["notes"] == "Great call"


def test_list_followups_counts_follow_ups_from_one(store):
    job, ids = job_with(store, ["Ana Ruiz"], sent_days_ago=5)
    sequence_since(store, job["id"], days_ago=10)
    out = text(call(build_server(store), "list_followups"))
    assert "follow-up 1 of 2" in out and "step 2 of 3" not in out


def test_sequence_form_adds_steps_and_previews_due_dates(store):
    job, _ = job_with(store, [])
    c = local_client(store)
    page = c.get(f"/jobs/{job['id']}").text
    assert "data-add-step" in page and "If you message someone today: Follow-up on" in page
    assert page.count('class="seq-step"') == 5 and page.count('class="seq-step" hidden') == 1
    assert "—" not in page
