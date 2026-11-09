"""Interviews month calendar, the week view kept as an alternate, and .ics files for the recruiter's own calendar."""
import re
from datetime import date, datetime, timezone

import pytest
from conftest import local_client

from talent import interviews, reports
from talent.store import set_shown_zone


@pytest.fixture(autouse=True)
def _zone():
    yield
    set_shown_zone(None)


def _job(store, name="Ana Ray"):
    job = store.add_job(title="Backend Engineer", company="Acme")
    cid = store.add_candidate(name=name)["id"]
    store.add_to_pipeline(cid, job["id"], "Interviews")
    return job["id"], cid


def _day(m, d):
    return next(x for w in m["weeks"] for x in w if x["day"] == d)


def test_month_grid_shape(store):
    reports.save_user_zone(store, "UTC")
    feb = interviews.month(store, date(2027, 2, 1), today=date(2027, 2, 10))
    assert [len(w) for w in feb["weeks"]] == [7] * 4  # Feb 2027 starts on a Monday and has 28 days
    assert feb["weeks"][0][0]["day"] == date(2027, 2, 1) and feb["weeks"][-1][-1]["day"] == date(2027, 2, 28)
    assert all(d["in_month"] for w in feb["weeks"] for d in w)
    aug = interviews.month(store, date(2027, 8, 15), today=date(2027, 8, 1))  # starts on a Sunday, 6 rows
    assert len(aug["weeks"]) == 6 and aug["first"] == date(2027, 8, 1)
    assert aug["weeks"][0][0]["day"] == date(2027, 7, 26) and not aug["weeks"][0][0]["in_month"]
    assert aug["weeks"][0][6]["day"] == date(2027, 8, 1) and aug["weeks"][0][6]["in_month"]
    assert aug["weeks"][-1][-1]["day"] == date(2027, 9, 5) and not aug["weeks"][-1][-1]["in_month"]
    assert [d["weekend"] for d in aug["weeks"][1]] == [False] * 5 + [True] * 2
    assert aug["prev"] == date(2027, 7, 1) and aug["next"] == date(2027, 9, 1)
    assert interviews.month(store, date(2026, 12, 1))["next"] == date(2027, 1, 1)


def test_today_is_marked(store):
    m = interviews.month(store, date(2027, 2, 1), today=date(2027, 2, 10))
    assert [d["day"] for w in m["weeks"] for d in w if d["today"]] == [date(2027, 2, 10)]
    html = local_client(store).get("/interviews").text
    assert 'class="cal-day' in html and " today" in html and 'aria-current="date"' in html


def test_late_evening_interview_stays_on_its_local_day(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    jid, cid = _job(store)
    store.log(cid, "interview", "Onsite: system design", at="2027-03-10T07:30:00+00:00", job_id=jid,
              interviewer="Kim Lee")  # 11:30 PM on Mar 9 in Los Angeles
    m = interviews.month(store, date(2027, 3, 1), today=date(2027, 3, 1))
    assert [x["interview_id"] for x in _day(m, date(2027, 3, 9))["sessions"]] != []
    assert _day(m, date(2027, 3, 10))["sessions"] == []
    html = local_client(store).get("/interviews?view=month&month=2027-03").text
    assert "11:30 PM" in html and "Ana Ray" in html
    # Sessions at the very start and end of the grid are found too.
    store.log(cid, "interview", "Intro", at="2027-04-01T06:59:00+00:00", job_id=jid)  # 11:59 PM Mar 31 in LA
    m = interviews.month(store, date(2027, 3, 1), today=date(2027, 3, 1))
    assert len(_day(m, date(2027, 3, 31))["sessions"]) == 1
    assert m["total"] == 2


def test_plus_n_more_links_to_that_week(store):
    reports.save_user_zone(store, "UTC")
    jid, cid = _job(store)
    for h in (9, 10, 11, 12, 13):
        store.log(cid, "interview", f"Round {h}", at=f"2027-02-10T{h:02d}:00:00+00:00", job_id=jid)
    m = interviews.month(store, date(2027, 2, 1), today=date(2027, 2, 1))
    d = _day(m, date(2027, 2, 10))
    assert len(d["sessions"]) == 5 and len(d["shown"]) == interviews.MONTH_CHIPS and d["more"] == 2
    html = local_client(store).get("/interviews?month=2027-02").text
    assert "+2 more" in html and "/interviews?view=week&amp;week=2027-02-08" in html
    assert html.count('class="cal-chip st-') >= 3


def test_states_and_cancelled_chips(store):
    reports.save_user_zone(store, "UTC")
    jid, cid = _job(store)
    past = store.log(cid, "interview", "Screen", at="2020-02-02T15:00:00+00:00", job_id=jid)
    done = store.log(cid, "interview", "Tech", at="2020-02-03T15:00:00+00:00", job_id=jid)
    store.update_interview(done["id"], outcome="passed")
    gone = store.log(cid, "interview", "Chat", at="2020-02-04T15:00:00+00:00", job_id=jid)
    store.update_interview(gone["id"], cancel=True)
    m = interviews.month(store, date(2020, 2, 1), today=date(2020, 2, 1))
    state = {x["interview_id"]: x["state"] for w in m["weeks"] for d in w for x in d["sessions"]}
    assert state[past["id"]] == "owes" and state[done["id"]] == "done" and state[gone["id"]] == "cancelled"
    html = local_client(store).get("/interviews?month=2020-02").text
    assert "st-cancelled" in html and "st-done" in html and "st-owes" in html
    assert f"/candidates/{cid}?iv={past['id']}#iv-{past['id']}" in html


def test_clash_is_flagged_on_the_chip(store):
    reports.save_user_zone(store, "UTC")
    jid, cid = _job(store)
    other = store.add_candidate(name="Bo Lin")["id"]
    store.log(cid, "interview", "A", at="2027-02-09T15:00:00+00:00", job_id=jid, interviewer="Kim Lee")
    store.log(other, "interview", "B", at="2027-02-09T15:20:00+00:00", job_id=jid, interviewer="Kim Lee")
    m = interviews.month(store, date(2027, 2, 1), today=date(2027, 2, 1))
    assert all(x["clash"] for x in _day(m, date(2027, 2, 9))["sessions"]) and len(m["clashes"]) == 1
    assert "1 clash to fix" in local_client(store).get("/interviews?month=2027-02").text


def test_prev_next_today_links_and_toggle(store):
    html = local_client(store).get("/interviews?view=month&month=2027-02").text
    assert "view=month&amp;month=2027-01" in html and "view=month&amp;month=2027-03" in html
    assert ">Today<" in html and "February 2027" in html
    assert "view=week" in html and ">Month<" in html and ">Week<" in html
    bad = local_client(store).get("/interviews?month=banana")
    assert bad.status_code == 200 and "Interviews" in bad.text


def test_week_view_still_works_with_week_param(store):
    reports.save_user_zone(store, "UTC")
    jid, cid = _job(store)
    store.log(cid, "interview", "Onsite", at="2027-02-10T15:00:00+00:00", job_id=jid, interviewer="Kim Lee")
    c = local_client(store)
    for url in ("/interviews?week=2027-02-08", "/interviews?view=week&week=2027-02-10"):
        html = c.get(url).text
        assert 'class="week' in html and "Previous week" in html and "Ana Ray" in html and "cal-day" not in html
    assert 'class="cal"' in c.get("/interviews").text  # month is the default


def test_calendar_help_and_recipe_on_the_page(store):
    html = local_client(store).get("/interviews").text
    assert "Download this month" in html and "/interviews.ics" in html
    assert "Interviews already in your calendar?" in html and 'data-recipe="run"' in html


def test_candidate_text_is_escaped_in_the_grid(store):
    reports.save_user_zone(store, "UTC")
    jid, cid = _job(store, name="<script>alert(1)</script>")
    store.log(cid, "interview", "<img src=x onerror=alert(1)>", at="2027-02-10T15:00:00+00:00", job_id=jid)
    html = local_client(store).get("/interviews?month=2027-02").text
    assert "<script>alert(1)</script>" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def _unfold(text):
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    lines = text.split("\r\n")[:-1]
    out = []
    for ln in lines:
        assert len(ln.encode()) <= 75
        if ln.startswith(" "):
            out[-1] += ln[1:]
        else:
            out.append(ln)
    return out


def test_ics_month_download(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    job = store.add_job(title="Eng, Platform; Core", company="Acme")
    cid = store.add_candidate(name="Lee, Ana; Jr")["id"]
    store.add_to_pipeline(cid, job["id"], "Interviews")
    a = store.log(cid, "interview", "Tech screen (30 min)\nsecret second line", at="2027-02-10T18:00:00+00:00",
                  job_id=job["id"], interviewer="Kim Lee")
    b = store.log(cid, "interview", "Final", at="2027-02-11T18:00:00+00:00", job_id=job["id"])
    store.update_interview(b["id"], cancel=True)
    store.log(cid, "interview", "March one", at="2027-03-05T18:00:00+00:00", job_id=job["id"])
    r = local_client(store).get("/interviews.ics?month=2027-02")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "attachment" in r.headers["content-disposition"] and ".ics" in r.headers["content-disposition"]
    lines = _unfold(r.text)
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-1] == "END:VCALENDAR" and "VERSION:2.0" in lines
    assert lines.count("BEGIN:VEVENT") == 2 == lines.count("END:VEVENT")
    assert f"UID:interview-{a['id']}@talent-search.local" in lines
    assert "DTSTART:20270210T180000Z" in lines and "DTEND:20270210T183000Z" in lines  # (30 min) in the summary
    assert "DTEND:20270211T184500Z" in lines  # no length given: 45 minutes
    assert "SUMMARY:Interview: Lee\\, Ana\\; Jr (Eng\\, Platform\\; Core\\, Acme)" in lines
    assert lines.count("STATUS:CANCELLED") == 1 and lines.count("STATUS:CONFIRMED") == 1
    assert "March one" not in r.text and "secret second line" not in r.text
    assert all(not ln.startswith("DESCRIPTION") or "second line" not in ln for ln in lines)
    # every line is NAME:value or NAME;params:value
    assert all(re.match(r"^[A-Z-]+[:;]", ln) for ln in lines)


def test_ics_upcoming_and_one_interview(store):
    reports.save_user_zone(store, "UTC")
    jid, cid = _job(store)
    soon = store.log(cid, "interview", "Screen", at="2999-01-05T15:00:00+00:00", job_id=jid)
    store.log(cid, "interview", "Old", at="2001-01-05T15:00:00+00:00", job_id=jid)
    c = local_client(store)
    all_up = c.get("/interviews.ics").text
    assert all_up.count("BEGIN:VEVENT") == 1 and f"interview-{soon['id']}@" in all_up
    one = c.get(f"/candidates/{cid}/interviews/{soon['id']}.ics")
    assert one.status_code == 200 and "DTSTART:29990105T150000Z" in one.text
    assert c.get(f"/candidates/{cid + 99}/interviews/{soon['id']}.ics").status_code == 404
    assert c.get("/interviews.ics?month=nope").status_code == 400
    page = c.get(f"/candidates/{cid}").text
    assert f"/candidates/{cid}/interviews/{soon['id']}.ics" in page and "Add to calendar" in page


def test_ics_folds_long_lines_and_escapes_backslashes():
    assert interviews._ics_text("a\\b;c,d\r\ne") == "a\\\\b\\;c\\,d\\ne"
    folded = interviews._ics_fold("SUMMARY:" + "é" * 100)
    assert all(len(p.encode()) <= 75 for p in folded) and "".join([folded[0], *(p[1:] for p in folded[1:])]) == "SUMMARY:" + "é" * 100
    assert datetime(2027, 1, 1, tzinfo=timezone.utc)
