"""Cycle 3, fixer I: client-safe status updates, close-out, interviews on exit, stage sets, wording, board."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from talent import actions
from talent.mcp_server import build_server

from conftest import local_client
from test_mcp import call, structured, text


def _at(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(second=0, microsecond=0)


def _msg(r, key="notice"):
    from conftest import flash_of
    return flash_of(r, key) or parse_qs(urlsplit(r.headers["location"]).query).get(key, [""])[0]


def _people(store, job_id, stage, *names):
    ids = []
    for n in names:
        cid = store.add_candidate(name=n)["id"]
        store.add_to_pipeline(cid, job_id, stage)
        ids.append(cid)
    return ids


# --------------------------------------------------------- 1. internal stages
def test_internal_stages_are_left_out_and_not_counted(store):
    job = store.add_job("Staff Backend Engineer", company="Acme Fintech", location="Remote")
    _people(store, job["id"], "Sourced", "Priya Raman", "Marcus Lee")
    _people(store, job["id"], "Contacted", "Kim Ode")
    _people(store, job["id"], "Interviewing", "Ana Ruiz")
    assert actions.saved_omit(store, job["id"]) == ["Sourced", "Contacted"]
    out = actions.job_status_text(store, job["id"])
    assert "Priya" not in out and "Marcus" not in out and "Kim Ode" not in out
    assert "1 candidate in process, none hired yet." in out and "Interviewing (1): Ana Ruiz" in out
    digest = actions.status_digest(store)
    assert "Priya" not in digest
    server = build_server(store)
    res = call(server, "job_pipeline", job_id=job["id"])
    assert "Priya" not in res.content[0].text and structured(res)["left_out"] == ["Sourced", "Contacted"]
    assert structured(res)["headline"].startswith("1 candidate in process")
    assert "Priya" not in text(call(server, "status_digest"))
    assert "Priya" in call(server, "job_pipeline", job_id=job["id"], omit_stages=[]).content[0].text
    c = local_client(store)
    page = c.get(f"/jobs/{job['id']}").text
    assert 'value="Sourced" checked' in page and "Left out until you choose: Sourced, Contacted" in page
    assert "Priya Raman" not in c.get("/jobs").text
    # Leave out still decides: saving nothing shows every stage, and that is remembered.
    c.post(f"/jobs/{job['id']}/omit", data={})
    assert actions.saved_omit(store, job["id"]) == [] and "Priya Raman" in actions.job_status_text(store, job["id"])


def test_only_people_in_internal_stages(store):
    job = store.add_job("Designer", company="Acme")
    _people(store, job["id"], "Sourced", "Priya Raman")
    out = actions.job_status_text(store, job["id"])
    assert "No one in process" in out and "No one to share yet." in out and "Priya" not in out
    assert actions.company_rollup(store)[0]["active"] == 0


# ------------------------------------------------------------ 2. wording
def test_names_capped_and_hired_wording_and_header(store):
    job = store.add_job("Backend Engineer", company="Acme", location="San Francisco (hybrid)")
    _people(store, job["id"], "Screening", *(f"Person {i:02}" for i in range(15)))
    _people(store, job["id"], "Hired", "Hana", "Ivo")
    out = actions.job_status_text(store, job["id"])
    assert out.splitlines()[0] == "Backend Engineer, Acme · San Francisco (hybrid)"
    line = next(x for x in out.splitlines() if x.startswith("Screening (15): "))
    assert line.endswith(", and 5 more") and line.count("Person") == 10
    assert "15 candidates in process, 2 hired, 1 opening. The role is filled." in out
    assert "2 of 1" not in out
    assert "opening filled" not in actions.status_digest(store, "acme")  # one company, one job: no roll-up line
    store.add_job("Second", company="Acme", openings=2)
    assert "2 open jobs, 15 in process, 1 of 3 openings filled." in actions.status_digest(store, "acme")


def test_digest_company_matching_single_company_and_on_hold(store):
    lumen = store.add_job("Senior Backend Engineer", company="Lumen Health")
    store.add_job("Product Designer", company="Lumen Health", status="on_hold")
    store.add_job("SRE", company="Ramp")
    _people(store, lumen["id"], "Screening", "Ana Ruiz")
    for q in ("lumen", "LUMEN HEALTH", "Lumen Helth", "health"):
        out = actions.status_digest(store, q)
        assert out.startswith("Status update for Lumen Health,"), q
        assert "SRE" not in out and "Ana Ruiz" in out
    out = actions.status_digest(store, "lumen")
    assert "- Lumen Health:" not in out and "1 open job" not in out  # no roll-up line repeating the one job
    assert out.splitlines()[-1] == "Product Designer, Lumen Health: on hold."
    assert "Product Designer, Lumen Health: on hold." in actions.status_digest(store)
    assert "No open jobs for Zed." in actions.status_digest(store, "Zed")
    data = structured(call(build_server(store), "status_digest", company="lumen"))
    assert data["companies"][0]["company"] == "Lumen Health" and data["on_hold"][0]["title"] == "Product Designer"


# ---------------------------------------------------- 3. stage sets and Out
def test_custom_stages_get_out_stages_and_stage_sets(store):
    c = local_client(store)
    r = c.post("/jobs", data={"title": "Founding Engineer", "stages": "Applied\nHM screen\nOnsite\nHired"},
               follow_redirects=False)
    jid = int(r.headers["location"].split("/")[2].split("?")[0])
    assert {s["name"]: s["outcome"] for s in store.job_stages(jid)}["Rejected"] == "lost"
    assert "Added Rejected and Withdrawn as Out stages" in _msg(r)
    r = c.post("/jobs", data={"title": "Exec search", "stage_set": "agency"}, follow_redirects=False)
    jid = int(r.headers["location"].split("/")[2].split("?")[0])
    assert [s["name"] for s in store.job_stages(jid)] == list(actions.STAGE_SETS["agency"][1])
    assert store.job_stages(jid)[5]["outcome"] == "hired" and "?" not in r.headers["location"]
    assert 'name="stage_set"' in c.get("/jobs/new").text and "In-house" in c.get("/jobs/new").text
    job = structured(call(build_server(store), "create_job", title="Data Lead", stages=["Applied", "Onsite", "Hired"]))
    assert job["stage_outcomes"]["Withdrawn"] == "lost" and "Out stages" in job["note"]
    kept = structured(call(build_server(store), "create_job", title="Ops", stages=["Sourced", "Hired", "Not a fit"]))
    assert "note" not in kept and "Rejected" not in kept["stages"]


# -------------------------------------------------------------- 4. close-out
def test_close_out_lists_people_and_moves_them_when_asked(store):
    job = store.add_job("Backend", company="Lumen")
    _people(store, job["id"], "Hired", "Hana")
    priya, bo = _people(store, job["id"], "Interviewing", "Priya Raman", "Bo Li")
    iv = store.log(priya, "interview", "Final", at=_at(3), job_id=job["id"])
    c = local_client(store)
    page = c.get(f"/jobs/{job['id']}").text
    assert "This role is filled. Close it?" in page and "Move the other 2 to Rejected (Position filled)" in page
    # The AI only hears about them.
    server = build_server(store)
    out = structured(call(server, "update_job", job_id=job["id"], status="closed"))
    assert {p["name"] for p in out["still_in_play"]} == {"Priya Raman", "Bo Li"}
    assert "Don't move anyone" in out["close_out_hint"]
    assert len(actions.in_play(store, job["id"])) == 2  # nobody moved
    page = c.get(f"/jobs/{job['id']}").text
    assert "This job is closed, but 2 people are still in play" in page and "1 upcoming interview" in page
    r = c.post(f"/jobs/{job['id']}/close-out", follow_redirects=False)
    assert "Moved 2 people to Rejected (Position filled) and cancelled 1 upcoming interview." in _msg(r)
    assert actions.in_play(store, job["id"]) == [] and store.get_interview(iv["id"])["status"] == "cancelled"
    notes = [t["body"] for t in store.timeline(bo) if t["kind"] == "note"]
    assert "Reason: Position filled" in notes
    assert "still in play" not in c.get(f"/jobs/{job['id']}").text
    assert "still_in_play" not in structured(call(server, "update_job", job_id=job["id"], status="closed"))


def test_closing_from_the_form_says_who_is_left(store):
    job = store.add_job("Backend", stages=["Applied", "Screen", "Hired"])
    _people(store, job["id"], "Screen", "Ana Ruiz")
    r = local_client(store).post(f"/jobs/{job['id']}", data={"status": "closed"}, follow_redirects=False)
    assert "1 person is still in play" in _msg(r)
    done = actions.close_out(store, job["id"])  # no Out stage on this old job: Rejected is added
    assert done["stage"] == "Rejected" and done["moved"] == 1


# ------------------------------------------------- 5. interviews on exit
def test_move_out_with_an_interview_asks_to_cancel(store):
    job = store.add_job("Founding Engineer", company="Lumen")
    dana = _people(store, job["id"], "Interviewing", "Dana Whit")[0]
    iv = store.log(dana, "interview", "Onsite", at=_at(2), job_id=job["id"])
    c = local_client(store)
    r = c.post(f"/jobs/{job['id']}/move", data={"candidate_id": dana, "stage": "Rejected", "view": "board"},
               headers={"HX-Request": "true"})
    day = actions.local_time(iv["at"], "%a %b %-d")
    assert f"Dana Whit has an interview {day}. Cancel it?" in r.text and "/cancel-interviews" in r.text
    r = c.post(f"/jobs/{job['id']}/cancel-interviews", data={"candidate_id": dana, "interview_id": [iv["id"], 99999]},
               follow_redirects=False)
    assert "Cancelled the interview." in _msg(r) and store.get_interview(iv["id"])["status"] == "cancelled"
    # Without htmx the page asks the same question.
    iv2 = store.log(dana, "interview", "Again", at=_at(4), job_id=job["id"])
    store.move_stage(dana, job["id"], "Interviewing")
    r = c.post(f"/jobs/{job['id']}/move", data={"candidate_id": dana, "stage": "Hired"}, follow_redirects=False)
    assert "ask_cancel" in r.headers["location"]
    assert "Cancel it?" in c.get(r.headers["location"]).text
    # The AI gets the list and a warning, and nothing is cancelled for it.
    store.move_stage(dana, job["id"], "Interviewing")
    out = structured(call(build_server(store), "move_stage", candidate_id=dana, job_id=job["id"], stage="Withdrawn"))
    assert out["upcoming_interviews"][0]["interview_id"] == iv2["id"] and "update_interview" in out["warning"]
    assert store.get_interview(iv2["id"])["status"] == "scheduled"
    store.move_stage(dana, job["id"], "Interviewing")
    assert "warning" not in structured(call(build_server(store), "move_stage", candidate_id=dana, job_id=job["id"],
                                            stage="Offer"))


def test_reason_picker_marks_the_reason_that_repeats_the_stage(store):
    job = store.add_job("Backend")
    _people(store, job["id"], "Screening", "Ana")
    page = local_client(store).get(f"/jobs/{job['id']}?view=board").text
    assert 'data-like="withdrew|withdrawn"' in page and ">Client passed<" in page


# --------------------------------------------------------- 6. in-house
def test_inhouse_wording_and_single_company(store):
    a = store.add_job("Backend", company="Lumen")
    store.add_job("SRE", company="Ramp")
    _people(store, a["id"], "Screening", "Ana")
    c = local_client(store)
    assert "Where each client stands" in c.get("/jobs").text
    r = c.post("/jobs/wording", data={"inhouse": "1"}, follow_redirects=False)
    assert "in-house" in _msg(r) and actions.inhouse(store)
    page = c.get("/jobs").text
    assert "Where each team stands" in page and "client stands" not in page
    assert ">Hiring team passed<" in c.get(f"/jobs/{a['id']}").text
    c.post("/jobs/wording", data={})
    assert not actions.inhouse(store) and ">Client passed<" in c.get(f"/jobs/{a['id']}").text
    store.update_job([j for j in store.list_jobs() if j["title"] == "SRE"][0]["id"], status="closed")
    page = c.get("/jobs").text
    assert "<h2>Status update</h2>" in page and "Copy status for all open jobs" in page and "<th>Company</th>" not in page


# ------------------------------------------------------------- 7. board
def test_board_caps_columns_and_folds_closed_ones(store):
    job = store.add_job("Backend")
    _people(store, job["id"], "Screening", *(f"P{i}" for i in range(55)))
    _people(store, job["id"], "Rejected", "Ro")
    c = local_client(store)
    page = c.get(f"/jobs/{job['id']}?view=board").text
    assert page.count('class="pc"') == 51 and "Show 5 more" in page and "show=Screening" in page
    assert '<details class="fold" ><summary class="fine">Show 1</summary>' in page
    page = c.get(f"/jobs/{job['id']}?view=board&show=Screening").text
    assert page.count('class="pc"') == 56 and "more</a>" not in page


# --------------------------------------------------------------- 8. thin
def test_thin_target_scales_with_openings(store):
    one = store.add_job("One", company="A")
    two = store.add_job("Two", company="B", openings=2)
    own = store.add_job("Own", company="C", openings=4, min_active=2)
    for j in (one, two, own):
        _people(store, j["id"], "Screening", "X", "Y")
    thin = next(i for i in actions.action_items(store)["items"] if i["kind"] == "thin")
    aims = {j["title"]: j["aim_min"] for j in thin["jobs"]}
    assert aims == {"One": 3, "Two": 5}  # 3 per opening, capped at 5; "Own" has its own target of 2
    store.set_settings(thin_min="4")
    thin = next(i for i in actions.action_items(store)["items"] if i["kind"] == "thin")
    assert {j["title"]: j["aim_min"] for j in thin["jobs"]} == {"One": 3, "Two": 4}


def test_no_dashes_in_new_copy(store):
    job = store.add_job("Backend", company="Acme")
    _people(store, job["id"], "Hired", "Hana")
    _people(store, job["id"], "Screening", "Ana")
    c = local_client(store)
    for url in ("/jobs", f"/jobs/{job['id']}", "/jobs/new"):
        assert "—" not in c.get(url).text and " – " not in c.get(url).text
    assert "—" not in actions.status_digest(store) and "—" not in str(actions.close_out_hint(store, job["id"]))
