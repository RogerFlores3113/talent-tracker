"""Fixer W: interviews month view and calendar files. Out-of-range days, time zone note, .ics freshness, weekend
sessions, counts, chips and the calendar's accessible markup."""
import re
from datetime import date, datetime, timedelta, timezone

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


@pytest.mark.parametrize("url", [
    "/interviews?month=9999-12", "/interviews?month=0001-01", "/interviews?week=9999-12-31",
    "/interviews?week=0001-01-01", "/interviews?view=week&week=9999-12-31T10:00:00Z",
    "/interviews?month=2201-01", "/interviews?month=1969-12"])
def test_out_of_range_pages_never_500(store, url):
    assert local_client(store).get(url).status_code == 200


@pytest.mark.parametrize("url", ["/interviews.ics?month=9999-12", "/interviews.ics?month=0001-01",
                                 "/interviews.ics?month=2201-01"])
def test_out_of_range_ics_is_400(store, url):
    r = local_client(store).get(url)
    assert r.status_code == 400 and "between" in r.text


def test_time_zone_note_links_to_settings(store):
    reports.save_user_zone(store, "UTC")
    c = local_client(store)
    for url in ("/interviews", "/interviews?view=week"):
        html = c.get(url).text
        assert "Times are in" in html and 'href="/settings#time-zone"' in html and "45 minutes" in html


def test_ics_has_sequence_and_last_modified_and_all_upcoming_skips_cancelled_and_finished(store):
    reports.save_user_zone(store, "UTC")
    jid, cid = _job(store)
    now = datetime.now(timezone.utc)
    live = store.log(cid, "interview", "Screen", at="2999-01-05T15:00:00+00:00", job_id=jid)
    gone = store.log(cid, "interview", "Final", at="2999-01-06T15:00:00+00:00", job_id=jid)
    store.update_interview(gone["id"], cancel=True)
    over = store.log(cid, "interview", "Earlier today (30 min)",
                     at=(now - timedelta(hours=3)).replace(microsecond=0).isoformat(), job_id=jid)
    c = local_client(store)
    text = c.get("/interviews.ics").text
    assert f"interview-{live['id']}@" in text
    assert f"interview-{gone['id']}@" not in text and f"interview-{over['id']}@" not in text
    assert re.search(r"SEQUENCE:\d+", text) and "LAST-MODIFIED:" in text and "DTSTAMP:" in text
    assert f"interview-{gone['id']}@" in c.get("/interviews.ics?cancelled=1").text
    store.update_interview(store.log(cid, "interview", "Old", at="2150-01-06T15:00:00+00:00", job_id=jid)["id"],
                           cancel=True)
    month = c.get("/interviews.ics?month=2150-01").text
    assert "STATUS:CANCELLED" in month
    earlier = interviews.ics_calendar([], now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "SEQUENCE" not in earlier


def test_sequence_goes_up(store):
    jid, cid = _job(store)
    t = store.log(cid, "interview", "Screen", at="2999-01-05T15:00:00+00:00", job_id=jid)
    row = store._row("""SELECT t.*, c.name AS candidate_name, NULL AS job_title, NULL AS job_company FROM timeline t
                        JOIN candidates c ON c.id = t.candidate_id WHERE t.id = ?""", (t["id"],))
    a = interviews.ics_event(row, datetime(2026, 11, 1, tzinfo=timezone.utc))
    b = interviews.ics_event(row, datetime(2026, 11, 2, tzinfo=timezone.utc))
    seq = lambda ev: int(next(x for x in ev if x.startswith("SEQUENCE:")).split(":")[1])
    assert a == b  # cycle 10 wave 2: an unchanged interview downloads the same whenever it is downloaded
    store.update_interview(t["id"], at="2999-01-06T15:00:00+00:00")
    row2 = store._row("""SELECT t.*, c.name AS candidate_name, NULL AS job_title, NULL AS job_company FROM timeline t
                         JOIN candidates c ON c.id = t.candidate_id WHERE t.id = ?""", (t["id"],))
    assert seq(interviews.ics_event(row2, datetime(2026, 11, 3, tzinfo=timezone.utc))) > seq(a)


def test_week_start_is_monday_on_or_before():
    assert interviews.week_start(date(2026, 11, 1)) == date(2026, 10, 26)  # a Sunday
    assert interviews.week_start(date(2026, 11, 7)) == date(2026, 11, 2)  # a Saturday
    assert interviews.week_start(date(2026, 11, 9)) == date(2026, 11, 9)


def test_sunday_session_shows_in_a_week(store):
    reports.save_user_zone(store, "UTC")
    jid, cid = _job(store)
    store.log(cid, "interview", "Sunday call", at="2026-11-01T15:00:00+00:00", job_id=jid)
    w = interviews.week(store, date(2026, 11, 1))
    assert w["total"] == 1 and w["start"] == date(2026, 10, 26) and w["days"][-1]["day"] == date(2026, 11, 1)


def test_month_count_ignores_cancelled_and_more_links_to_the_day(store):
    reports.save_user_zone(store, "UTC")
    jid, cid = _job(store)
    for h in range(9, 14):
        store.log(cid, "interview", f"Round {h}", at=f"2027-02-10T{h:02d}:00:00+00:00", job_id=jid,
                  interviewer=f"Pat {h}")
    x = store.log(cid, "interview", "Dropped", at="2027-02-11T09:00:00+00:00", job_id=jid)
    store.update_interview(x["id"], cancel=True)
    m = interviews.month(store, date(2027, 2, 1), today=date(2027, 2, 2))
    assert m["total"] == 5
    html = local_client(store).get("/interviews?month=2027-02").text
    assert "5 sessions" in html
    assert 'href="/interviews?view=week&amp;week=2027-02-10#day-2027-02-10"' in html
    assert "+2 more" in html
    week = local_client(store).get("/interviews?week=2027-02-10").text
    assert 'id="day-2027-02-10"' in week
    # same person five times that day: chips say which session
    assert 'class="cal-tag">Pat 9' in html


def test_calendar_markup_is_accessible(store):
    reports.save_user_zone(store, "UTC")
    jid, cid = _job(store)
    store.log(cid, "interview", "Screen", at="2027-02-10T15:00:00+00:00", job_id=jid)
    old = store.log(cid, "interview", "Final", at="2027-02-11T15:00:00+00:00", job_id=jid)
    store.update_interview(old["id"], cancel=True)
    html = local_client(store).get("/interviews?month=2027-02").text
    assert 'role="grid"' not in html and "gridcell" not in html and 'role="row"' not in html
    assert re.search(r"week of Feb 8", html)  # the day number link has a real name
    assert re.search(r'class="cal-vh">Upcoming: </span>', html) and 'class="cal-vh">Cancelled: </span>' in html
    assert "Previous month: </span>January 2027" in html and "Next month: </span>March 2027" in html
    jan = local_client(store).get("/interviews?month=2027-01").text
    assert "Dec 28" in jan  # days of neighbouring months carry the month
    assert 'class="cal-nav"' in html
    css = open(__import__("talent").__path__[0] + "/static/style.css").read()
    assert not re.search(r"\.cal-day\.other\s*\{[^}]*opacity", css)
