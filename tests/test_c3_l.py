"""Cycle 3, fixer L: close-out wording and undo, company matching, honest status text, client title, Add fills."""
from datetime import datetime, timedelta, timezone

from talent import actions

from conftest import flash_of, local_client


def _at(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(second=0, microsecond=0)


def _people(store, job_id, stage, *names):
    ids = []
    for n in names:
        cid = store.add_candidate(name=n)["id"]
        store.add_to_pipeline(cid, job_id, stage)
        ids.append(cid)
    return ids


def _stage(store, cid, job_id):
    return next(p["stage"] for p in store.pipeline(job_id) if p["candidate_id"] == cid)


# --------------------------------------------------------- 1. close-out wording
def test_close_out_says_position_filled_and_offers_to_close_the_job(store):
    job = store.add_job("Staff Backend Engineer", company="Acme")
    _people(store, job["id"], "Hired", "Hana Ito")
    priya = _people(store, job["id"], "Interviewing", "Priya Raman")[0]
    tom = _people(store, job["id"], "Screening", "Tom Nguyen")[0]
    store.move_stage(tom, job["id"], "Rejected")
    c = local_client(store)
    page = c.get(f"/jobs/{job['id']}").text
    assert "Move the other 1 to Rejected (Position filled)</button>" in page  # no interviews: no "and cancel"
    assert 'name="close_job" value="1" checked' in page
    r = c.post(f"/jobs/{job['id']}/close-out", data={"close_job": "1"}, follow_redirects=False)
    assert "The job is closed." in flash_of(r) and store.get_job(job["id"])["status"] == "closed"
    out = actions.job_status_text(store, job["id"])
    out_line = next(x for x in out.splitlines() if x.startswith("- Out: "))
    assert "Tom Nguyen (Rejected)" in out_line and "Priya Raman (position filled)" in out_line
    assert "Priya Raman (Rejected)" not in out and "Priya Raman (position filled, today)" in out
    assert "Tom Nguyen (today)" in out


def test_close_out_button_names_interviews_only_when_there_are_some(store):
    job = store.add_job("Backend", company="Lumen")
    _people(store, job["id"], "Hired", "Hana")
    priya = _people(store, job["id"], "Interviewing", "Priya Raman")[0]
    store.log(priya, "interview", "Final", at=_at(3), job_id=job["id"])
    page = local_client(store).get(f"/jobs/{job['id']}").text
    assert "(Position filled) and cancel their upcoming interviews</button>" in page


# ----------------------------------------------------------- 2. close-out undo
def test_close_out_is_one_change_with_its_own_undo(store):
    job = store.add_job("Staff Backend Engineer", company="Acme")
    _people(store, job["id"], "Hired", "Hana")
    priya, bo, kim = _people(store, job["id"], "Interviewing", "Priya Raman", "Bo Li", "Kim Ode")
    store.move_stage(kim, job["id"], "Offer")
    iv = store.log(priya, "interview", "Final", at=_at(3), job_id=job["id"])
    c = local_client(store)
    r = c.post(f"/jobs/{job['id']}/close-out", data={"close_job": "1"}, follow_redirects=False)
    assert "undo this close-out from Changes" in flash_of(r)
    page = c.get("/changes").text
    assert ("Closed out Staff Backend Engineer (Acme): moved 3 people to Rejected, cancelled 1 interview "
            "and closed the job") in page
    assert page.count("Undo this close-out") == 1 and "/changes/close-out/1/undo" in page
    assert "Undo this close-out" not in c.get("/changes?who=ai").text
    store.move_stage(bo, job["id"], "Withdrawn")  # moved again since: stays put
    r = c.post("/changes/close-out/1/undo", follow_redirects=False)
    msg = flash_of(r)
    assert "Moved 2 people back" in msg and "put back 1 interview" in msg and "1 person was moved again" in msg
    assert "The job is open again." in msg
    assert _stage(store, priya, job["id"]) == "Interviewing" and _stage(store, kim, job["id"]) == "Offer"
    assert _stage(store, bo, job["id"]) == "Withdrawn"
    assert store.get_interview(iv["id"])["status"] != "cancelled" and store.get_job(job["id"])["status"] == "open"
    assert next(p for p in store.pipeline(job["id"]) if p["candidate_id"] == priya)["notes"] != actions.CLOSE_REASON
    page = c.get("/changes").text
    assert "Undo this close-out" not in page and "Undone" in page
    assert "already undone" in flash_of(c.post("/changes/close-out/1/undo", follow_redirects=False), "flash_error")
    assert "gone" in flash_of(c.post("/changes/close-out/9/undo", follow_redirects=False), "flash_error")


# ------------------------------------------------------- 3. company matching
def test_company_match_ignores_case_punctuation_and_suffixes(store):
    plaid = store.add_job("Backend", company="Plaid")
    acme = store.add_job("Designer", company="Acme")
    _people(store, plaid["id"], "Screening", "Ana Ruiz")
    _people(store, acme["id"], "Screening", "Bo Li")
    for q in ("Plaid, Inc.", "plaid inc", "PLAID LLC", "Plaid Technologies"):
        out = actions.status_digest(store, q)
        assert "Ana Ruiz" in out and "Bo Li" not in out, q
    for q in ("acme inc", "Acme Corp", "Acme, Ltd.", "acme co"):
        assert "Bo Li" in actions.status_digest(store, q), q
    assert actions.company_key("Plaid, Inc.") == "plaid" and actions.company_key("Inc") == "inc"
    assert actions.status_digest(store, "Zed, Inc.") == "No open jobs for Zed, Inc."
    store.add_job("Ops", company="ACME Inc.")
    assert len(actions.company_rollup(store)) == 2  # Acme and ACME Inc. are one client


# ---------------------------------------------------- 4. status text honesty
def test_status_text_says_what_it_leaves_out_and_when_seats_are_filled(store):
    job = store.add_job("Backend", company="Acme")
    _people(store, job["id"], "Sourced", *(f"S{i}" for i in range(3)))
    _people(store, job["id"], "Contacted", "C1")
    _people(store, job["id"], "Hired", "Hana")
    out = actions.job_status_text(store, job["id"])
    assert "1 of 1 opening filled." in out and "No one in process" not in out and "role is filled" not in out
    assert "Not shown: Sourced (3), Contacted (1)" in out and "S1" not in out
    _people(store, job["id"], "Screening", "Ana Ruiz")
    out = actions.job_status_text(store, job["id"])
    assert "1 candidate in process, 1 of 1 opening filled." in out and "role is filled" not in out
    assert "Not shown" not in actions.job_status_text(store, job["id"], omit_stages=[])
    other = store.add_job("Data", company="Acme", stages=["Applied", "Screen", "Hired"])
    _people(store, other["id"], "Applied", "Applicant One")
    assert actions.saved_omit(store, other["id"]) == []
    assert "Applied (1): Applicant One" in actions.job_status_text(store, other["id"])


# ------------------------------------------------------- 5. client title
def test_status_box_is_for_the_client_unless_in_house(store):
    job = store.add_job("Backend", company="Acme")
    c = local_client(store)
    assert "Status update for the client</summary>" in c.get(f"/jobs/{job['id']}").text
    actions.set_inhouse(store, True)
    assert "Status update for the hiring manager</summary>" in c.get(f"/jobs/{job['id']}").text


# ------------------------------------------------- 6. web Add same-name box
def test_add_them_fills_empty_fields_and_says_which(store):
    tom = store.add_candidate(name="Tom Nguyen", current_company="Stripe")
    store.add_candidate(name="Other Person", email="taken@acme.com")
    job = store.add_job("Staff Backend Engineer", "Acme")
    c = local_client(store)
    form = {"name": "Thomas Nguyen", "email": "thomas@acme.com", "current_company": "Acme",
            "location": "Austin", "job_id": str(job["id"])}
    r = c.post("/candidates", data=form, follow_redirects=False)
    assert "a similar name to Thomas Nguyen" in r.text
    assert 'name="fill_email" value="thomas@acme.com"' in r.text and 'name="fill_current_company"' not in r.text
    r = c.post(f"/jobs/{job['id']}/add", data={"candidate_id": tom["id"], "fill_email": "thomas@acme.com",
                                               "fill_location": "Austin", "fill_current_company": "Acme"},
               follow_redirects=False)
    assert flash_of(r) == "Added Tom Nguyen to this job and filled in their email and location."
    got = store.get_candidate(tom["id"])
    assert got["email"] == "thomas@acme.com" and got["location"] == "Austin" and got["current_company"] == "Stripe"
    assert any(p["candidate_id"] == tom["id"] for p in store.pipeline(job["id"]))
    # An email another person has is left out, and the notice says so.
    lee = store.add_candidate(name="Lee Park")
    r = c.post(f"/jobs/{job['id']}/add", data={"candidate_id": lee["id"], "fill_email": "taken@acme.com",
                                               "fill_phone": "555 0100"}, follow_redirects=False)
    assert "filled in their phone" in flash_of(r) and "Didn't fill the email" in flash_of(r)
    priya = store.add_candidate(name="Priya Raman")
    r = c.post("/candidates", data={"name": "Priya Raman", "job_id": str(job["id"])}, follow_redirects=False)
    assert "the same name as Priya Raman" in r.text and priya["id"]
