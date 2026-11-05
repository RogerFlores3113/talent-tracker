"""Cycle 5 re-review fixer I: Decide safety, interviewer verdicts, in-house stage defaults, close_job."""
import json
import re
from datetime import datetime, time, timedelta, timezone

from conftest import flash_of, local_client
from talent import actions, decide, reports
from talent.mcp_server import build_server
from talent.store import interview_label, interview_note, now_iso

from test_mcp import call, structured, text

INHOUSE = list(actions.STAGE_SETS["inhouse"][1])


def _local(day_offset: int, hour: int, minute: int = 0) -> str:
    tz = datetime.now(timezone.utc).astimezone().tzinfo
    day = datetime.now(timezone.utc).astimezone().date() + timedelta(days=day_offset)
    return datetime.combine(day, time(hour, minute), tz).isoformat()


def _three(store):
    """Three finalists; Jordan still has a 4-session onsite ahead."""
    job = store.add_job("Clinical PM", "Lumen Health", stages=INHOUSE)
    people = [store.add_candidate(name=n) for n in ("Mei Lin Zhao", "Jordan Ellis", "Tomas Weber")]
    for c in people:
        store.add_to_pipeline(c["id"], job["id"], stage="Interviews")
    mei, jordan, tomas = people
    for c in (mei, tomas):
        iv = store.log(c["id"], "interview", "HM screen", at=_local(-3, 10), job_id=job["id"], interviewer="Priya")
        store.update_interview(iv["id"], outcome="yes")
    for h in (9, 10, 11, 13):
        store.log(jordan["id"], "interview", f"Onsite {h}", at=_local(3, h), job_id=job["id"], interviewer="Lee Park")
    return job, mei, jordan, tomas


# ------------------------------------------- 1. Decide starts with nobody passed on
def test_decide_pass_boxes_start_unticked_and_name_sessions_still_to_come(store):
    job, mei, jordan, tomas = _three(store)
    d = decide.finalists(store, job["id"])
    by = {p["name"]: p for p in d["people"]}
    assert by["Jordan Ellis"]["still_to_come"] == 4 and by["Mei Lin Zhao"]["still_to_come"] == 0
    page = local_client(store).get(f"/jobs/{job['id']}/decide").text
    boxes = re.findall(r'<input type="checkbox" name="pass"[^>]*>', page)
    assert len(boxes) == 3 and not any("checked" in b for b in boxes)
    assert "Jordan Ellis has 4 interviews still to come" in page
    assert f'value="{jordan["id"]}" data-name="Jordan Ellis" data-ahead="4"' in page
    assert 'data-decide="Rejected"' in page and "data-confirm=" not in page.split('id="record"')[1]
    # Coming-up sessions have no Add feedback; past ones do.
    assert page.count('<summary class="link small">Add feedback</summary>') == 2
    body = text(call(build_server(store), "compare_finalists", job_id=job["id"]))
    assert "4 interviews still to come" in body
    # Nobody ticked: the offer goes out and everyone else stays put.
    r = local_client(store).post(f"/jobs/{job['id']}/decide", data={"offer": mei["id"], "reason": "Not a fit", "confirmed": "1"},
                                 follow_redirects=False)
    assert flash_of(r).startswith('Recorded: Offer to "Mei Lin Zhao".')
    stages = {p["candidate_id"]: p["stage"] for p in store.pipeline(job["id"])}
    assert stages[jordan["id"]] == "Interviews" and stages[tomas["id"]] == "Interviews"
    assert len(actions.upcoming_for(store, jordan["id"], job["id"])) == 4


def test_the_confirm_script_lists_who_is_passed_on():
    js = open("src/talent/static/app.js").read()
    assert "data-decide" in js and "decideQuestion" in js and "still to come" in js


def test_compare_finalists_fence_carries_a_fresh_token(store):
    job, mei, jordan, tomas = _three(store)
    server = build_server(store)
    a = text(call(server, "compare_finalists", job_id=job["id"]))
    b = text(call(server, "compare_finalists", job_id=job["id"]))
    ta = re.search(re.escape(decide.BEGIN) + r" \[(\w+)\]", a).group(1)
    tb = re.search(re.escape(decide.BEGIN) + r" \[(\w+)\]", b).group(1)
    assert ta != tb and f"{decide.END} [{ta}]" in a and a.count(decide.END) == 1


# ------------------------------------------- 2. referrer needs from, by or a colon
def test_referrer_needs_from_by_or_colon():
    assert decide.referrer("Referral") is None
    assert decide.referrer("referral") is None and decide.referrer(None) is None
    assert decide.referrer("Referral: Sam Lee") == "Sam Lee"
    assert decide.referrer("Referred by Sam Lee") == "Sam Lee"
    assert decide.referrer("referral from Sam") == "Sam"
    assert decide.referrer("Referral from Jen Park.") == "Jen Park"
    assert decide.referrer("Referrals page") is None and decide.referrer("LinkedIn") is None


def test_plain_referral_source_shows_no_referrer(store):
    job = store.add_job("PM", "Lumen", stages=INHOUSE)
    c = store.add_candidate(name="Ana Ruiz", source="Referral")
    store.add_to_pipeline(c["id"], job["id"], stage="Interviews")
    page = local_client(store).get(f"/jobs/{job['id']}/decide").text
    assert "Found via Referral" in page and "referred by" not in page
    assert "referred by" not in text(call(build_server(store), "compare_finalists", job_id=job["id"]))


# ------------------------------------------- 3. the interviewer's verdict is its own outcome
def test_mixed_is_kept_as_mixed_everywhere(store):
    job = store.add_job("PM", "Lumen", stages=INHOUSE)
    c = store.add_candidate(name="Tomas Weber")
    store.add_to_pipeline(c["id"], job["id"], stage="Hiring manager screen")
    iv = store.log(c["id"], "interview", "HM screen", at=_local(-1, 10), job_id=job["id"], interviewer="Dana")
    server = build_server(store)
    out = call(server, "update_interview", interview_id=iv["id"], outcome="mixed", note="Good domain, slow")
    assert "as Mixed" in text(out) and structured(out)["interview"]["result"] == "Mixed"
    got = store.get_interview(iv["id"])
    assert got["status"] == "mixed" and got["data"]["verdict"] == "mixed"
    assert got["data"]["outcome_note"] == "Good domain, slow"
    # Decide, compare_finalists, Today, the profile and get_candidate all say Mixed, not Passed.
    d = decide.finalists(store, job["id"])
    assert d["people"][0]["interviews"][0]["outcome"] == "Mixed"
    client = local_client(store)
    assert '<span class="badge">Mixed</span>' in client.get(f"/jobs/{job['id']}/decide").text
    assert "Mixed. How it went: Good domain, slow" in text(call(server, "compare_finalists", job_id=job["id"]))
    item = next(i for i in actions.action_items(store)["items"] if i["kind"] == "interview_result")
    assert item["title"] == "Tomas Weber got mixed feedback on HM screen" and item["next_stage"] is None
    today = client.get("/").text
    assert "got mixed feedback on HM screen" in today and "Compare finalists" in today
    assert "Move to Interviews" not in today
    profile = client.get(f"/candidates/{c['id']}").text
    assert '<span class="badge">Mixed</span>' in profile and '<option value="mixed" selected>' in profile
    cand = text(call(server, "get_candidate", candidate_id=c["id"]))
    assert "(interview id %d, mixed, with Dana)" % iv["id"] in cand and "passed" not in cand.lower()


def test_strong_yes_shows_its_word_and_still_counts_as_passed(store):
    job = store.add_job("PM", "Lumen", stages=INHOUSE)
    c = store.add_candidate(name="Mei Lin Zhao")
    store.add_to_pipeline(c["id"], job["id"], stage="Hiring manager screen")
    iv = store.log(c["id"], "interview", "HM screen", at=_local(-1, 10), job_id=job["id"])
    client = local_client(store)
    r = client.post(f"/candidates/{c['id']}/interviews/{iv['id']}",
                    data={"op": "outcome", "outcome": "strong yes", "next": "/"}, follow_redirects=False)
    assert flash_of(r) == "Saved: Strong yes for Mei Lin Zhao."
    assert store.get_interview(iv["id"])["status"] == "passed"
    item = next(i for i in actions.action_items(store)["items"] if i["kind"] == "interview_result")
    assert item["title"] == "Mei Lin Zhao passed HM screen (Strong yes)" and item["next_stage"] == "Interviews"


def test_old_stored_verdicts_still_read_right(store):
    job = store.add_job("PM", "Lumen", stages=INHOUSE)
    c = store.add_candidate(name="Old Record")
    store.add_to_pipeline(c["id"], job["id"], stage="Interviews")
    iv = store.log(c["id"], "interview", "Screen", at=_local(-2, 10), job_id=job["id"])
    old = {"outcome": "passed", "outcome_at": now_iso(), "outcome_note": "Mixed. Unsure about scope"}
    with store._lock, store._conn:
        store._conn.execute("UPDATE timeline SET data = ? WHERE id = ?", (json.dumps(old), iv["id"]))
    got = store.get_interview(iv["id"])
    assert got["status"] == "mixed" and interview_label(got["data"]) == "Mixed"
    assert interview_note(got["data"]) == "Unsure about scope"
    assert decide.finalists(store, job["id"])["people"][0]["interviews"][0]["outcome"] == "Mixed"
    # A plain old pass, and a note that only starts with the word, stay as they were.
    assert interview_label({"outcome": "passed", "outcome_note": "Mixed on system design"}) == "Passed"
    assert interview_label({"outcome": "not_passed", "outcome_note": "Strong no."}) == "Strong no"
    assert interview_label({"outcome": "passed", "outcome_note": "No."}) == "Passed"
    # Recording a new outcome drops the old word from the note.
    store.update_interview(iv["id"], outcome="yes")
    got = store.get_interview(iv["id"])
    assert got["data"]["outcome_note"] == "Unsure about scope" and interview_label(got["data"]) == "Yes"


def test_no_double_period_in_timeline_lines(store):
    job = store.add_job("PM", "Lumen", stages=INHOUSE)
    c = store.add_candidate(name="Ana Ruiz")
    store.add_to_pipeline(c["id"], job["id"], stage="Interviews")
    iv = store.log(c["id"], "interview", "Screen", at=_local(-2, 10), job_id=job["id"])
    store.update_interview(iv["id"], outcome="yes", note="Clear thinker.")
    store.add_interview_feedback(iv["id"], "Great.", "Sam")
    body = text(call(build_server(store), "get_candidate", candidate_id=c["id"]))
    assert ".." not in body and "yes)" in body and "How it went: Clear thinker. Feedback from Sam: Great." in body


# ------------------------------------------- 4. in-house mode picks the in-house stages
def test_inhouse_mode_defaults_new_jobs_to_inhouse_stages(store):
    client = local_client(store)
    assert '<option value="default" selected' in client.get("/jobs/new").text
    actions.set_inhouse(store, True)
    assert '<option value="inhouse" selected' in client.get("/jobs/new").text
    r = client.post("/jobs", data={"title": "Designer", "company": "Lumen", "stage_set": "inhouse"},
                    follow_redirects=False)
    jid = int(r.headers["location"].split("/jobs/")[1].split("?")[0])
    assert [s["name"] for s in store.job_stages(jid)] == INHOUSE
    assert reports.stage_groups(store, jid)["hiring manager screen"] == "screening"
    out = structured(call(build_server(store), "create_job", title="Backend", company="Lumen"))
    jid2 = out["id"]
    assert [s["name"] for s in store.job_stages(jid2)] == INHOUSE
    assert reports.stage_groups(store, jid2)["hiring manager screen"] == "screening"
    # An explicit set or own stages still win.
    out = structured(call(build_server(store), "create_job", title="Ops", stage_set="default"))
    assert store.job_stages(out["id"])[0]["name"] == "Sourced"


def test_agency_mode_keeps_default_stages_and_hm_screen_as_interview(store):
    out = structured(call(build_server(store), "create_job", title="Backend", company="Acme"))
    assert store.job_stages(out["id"])[0]["name"] == "Sourced"
    job = store.add_job("X", "Y", stages=INHOUSE)  # not made from the set: name rules apply
    assert reports.stage_groups(store, job["id"])["hiring manager screen"] == "interview"


# ------------------------------------------- 5. close_job alias
def test_close_job_is_update_job_status_closed(store):
    job = store.add_job("Designer", "Lumen")
    out = call(build_server(store), "close_job", job_id=job["id"])
    assert store.get_job(job["id"])["status"] == "closed"
    assert "closed" in text(out).lower()
