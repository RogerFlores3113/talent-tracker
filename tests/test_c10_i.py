"""Cycle 10 fixer I: time zones per person, DST gaps and fall-backs, all-day interviews, unique .ics titles, the week's
.ics, clash text, Decide's finalists, merged feedback cards, saved interviewer emails and loop holidays."""
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote

import pytest
from conftest import flash_of, local_client

from talent import actions, decide, interviews, loops, reports
from talent.store import parse_when, resolve_when, set_shown_zone


@pytest.fixture(autouse=True)
def _zone():
    yield
    set_shown_zone(None)


def _job(store, name="Ana Ray", stage="Interviews"):
    job = store.add_job(title="Backend Engineer", company="Acme")
    cid = store.add_candidate(name=name)["id"]
    store.add_to_pipeline(cid, job["id"], stage)
    return job["id"], cid


def _unfold(text):
    return text.replace("\r\n ", "").split("\r\n")


# ------------------------------------------------------------ DST
def test_a_time_in_the_spring_forward_gap_is_refused_with_the_nearest_real_times():
    with pytest.raises(ValueError) as e:
        parse_when("2027-03-14 2:30 AM America/New_York")
    assert str(e.value) == ("2:30 AM doesn't exist on Mar 14 in New York; clocks jump from 2:00 to 3:00. "
                            "Pick 3:30 AM or 1:30 AM?")
    with pytest.raises(ValueError, match="1:30 AM doesn't exist on Mar 28 in London"):
        parse_when("2027-03-28 1:30 Europe/London")
    assert parse_when("2027-03-14 3:30 AM America/New_York") == "2027-03-14T07:30:00+00:00"
    assert parse_when("2027-03-14 1:30 AM America/New_York") == "2027-03-14T06:30:00+00:00"


def test_the_gap_applies_on_the_recruiters_own_clock_too(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    with pytest.raises(ValueError, match="2:30 AM doesn't exist on Mar 14 in Los Angeles"):
        parse_when("2027-03-14T02:30")
    cid = store.add_candidate(name="Gap Person")["id"]
    with pytest.raises(ValueError, match="doesn't exist"):
        store.log(cid, "interview", "Screen", at="2027-03-14T02:30")
    assert _ivs(store, cid) == []  # nothing was saved


def test_an_ambiguous_fall_back_time_takes_the_first_and_says_so(store):
    w = resolve_when("2027-11-07 1:30 AM America/New_York")
    assert w.iso == "2027-11-07T05:30:00+00:00"  # the first one, 1:30 EDT
    assert len(w.notes) == 1 and "happens twice" in w.notes[0] and "first one (EDT)" in w.notes[0]
    assert "2027-11-07T01:30-05:00" in w.notes[0]  # how to ask for the second
    assert parse_when("2027-11-07T01:30:00-05:00") == "2027-11-07T06:30:00+00:00"
    cid = store.add_candidate(name="Fall Back")["id"]
    entry = store.log(cid, "interview", "Screen", at="2027-11-07 1:30 AM America/New_York")
    assert entry["when_notes"] and entry["at"] == "2027-11-07T05:30:00+00:00"


def test_the_web_form_refuses_a_gap_time_with_a_friendly_message(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    _jid, cid = _job(store)
    c = local_client(store)
    r = c.post(f"/candidates/{cid}/log", data={"kind": "interview", "body": "Screen", "at": "2027-03-14T02:30"},
               follow_redirects=False)
    assert "doesn't exist on Mar 14" in flash_of(r, "error")
    assert _ivs(store, cid) == []


def test_an_unknown_zone_abbreviation_says_so_and_suggests_a_name():
    with pytest.raises(ValueError, match="Asia/Kolkata"):
        parse_when("2026-12-10 9:00 IST")


# ------------------------------------------------------------ date-only
def test_a_date_only_interview_is_all_day_not_midnight_utc(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    _jid, cid = _job(store)
    entry = store.log(cid, "interview", "Onsite", at="2026-12-03")
    assert entry["at"] == "2026-12-03T20:00:00+00:00"  # midday on the 3rd in Los Angeles, not 00:00Z
    assert actions._iv_data(entry)["all_day"] is True
    assert actions.local_time(entry["at"], "%a %b %-d") == "Thu Dec 3"
    ics = interviews.ics_for_month(store, date(2026, 12, 1))
    lines = _unfold(ics)
    assert "DTSTART;VALUE=DATE:20261203" in lines and "DTEND;VALUE=DATE:20261204" in lines
    assert not any(ln.startswith("DTSTART:") for ln in lines)
    page = local_client(store).get("/interviews?month=2026-12").text
    assert "All day" in page


def test_a_dated_time_is_not_all_day(store):
    _jid, cid = _job(store)
    entry = store.log(cid, "interview", "Onsite", at="2026-12-03 9:00 AM PT")
    assert "all_day" not in actions._iv_data(entry)
    entry2 = store.log(cid, "interview", "Other", at="2026-12-03T00:00:00+00:00")
    assert "all_day" not in actions._iv_data(entry2)


def test_the_web_form_needs_a_time_for_an_interview(store):
    _jid, cid = _job(store)
    c = local_client(store)
    r = c.post(f"/candidates/{cid}/log", data={"kind": "interview", "body": "Screen", "at": ""}, follow_redirects=False)
    assert "Pick the date and time" in flash_of(r, "error")
    assert _ivs(store, cid) == []
    r = c.post(f"/candidates/{cid}/log", data={"kind": "note", "body": "hello", "at": ""}, follow_redirects=False)
    assert any(t["kind"] == "note" for t in store.timeline(cid))  # other kinds still default to now


# ------------------------------------------------------------ per-person zones
def test_when_for_shows_the_readers_clock_first_and_mine_in_brackets(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    at = "2026-11-10T17:00:00+00:00"  # 9:00 AM PST
    assert interviews.when_for(at, "America/New_York") == "Tue Nov 10, 12:00 PM EST (9:00 AM PST)"
    assert interviews.when_for(at, None) == "Tue Nov 10, 9:00 AM PST"
    assert interviews.when_for(at, "America/Los_Angeles") == "Tue Nov 10, 9:00 AM PST"
    assert interviews.when_for("2026-11-10T22:00:00+00:00", "Europe/London") == "Tue Nov 10, 10:00 PM GMT (2:00 PM PST)"
    assert interviews.when_for("2026-11-10T22:00:00+00:00", "Asia/Kolkata").startswith("Wed Nov 11, 3:30 AM IST (")
    assert interviews.zone_label(datetime(2026, 11, 10, tzinfo=timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/Argentina/Buenos_Aires"))) == "UTC-3"


def test_log_with_zones_saves_them_and_invites_use_them(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    jid, cid = _job(store, "Noah Atlanta")
    store.save_interviewer("Dana Lee", "dana@x.com", "Europe/London")
    store.set_candidate_zone(cid, "America/New_York")
    store.save_interviewer("Sam Roe", None, None)
    loops.save_template(store, jid, "System design | 60 | Dana Lee", "9-17")
    p = loops.plan(store, jid, cid, start="2026-11-10", now=datetime(2026, 11, 5, tzinfo=timezone.utc))
    s = p["sessions"][0]
    assert s["at_local"].endswith("PST") and "9:00 AM" in s["at_local"]
    assert s["at_for_candidate"] == "Tue Nov 10, 12:00 PM EST (9:00 AM PST)"
    assert s["at_for_interviewer"].startswith("Tue Nov 10, 5:00 PM GMT (")
    assert "5:00 PM GMT" in p["invites"][0]["body"] and p["invites"][0]["mailto"].startswith("mailto:dana@x.com?")
    assert "12:00 PM EST (9:00 AM PST)" in p["candidate_invite"]["body"]
    assert any("5:00 PM" in w or "late" in w for w in s["warnings"]) is False  # 5 PM is a fine hour
    loops.book(store, cid, jid, loops.slots_from(p), by="app", candidate_tz="America/New_York")
    assert store.candidate_zone(cid) == "America/New_York"
    iv = _ivs(store, cid)[0]
    assert iv["data"]["candidate_tz"] == "America/New_York"


def test_an_odd_hour_for_someone_in_another_zone_is_warned_about(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    jid, cid = _job(store)
    store.save_interviewer("Dana Lee", None, "Asia/Kolkata")
    loops.save_template(store, jid, "System design | 60 | Dana Lee", "9-17")
    p = loops.plan(store, jid, cid, start="2026-11-10", now=datetime(2026, 11, 5, tzinfo=timezone.utc))
    assert any("Dana Lee would be at" in w and "late" in w for w in p["sessions"][0]["warnings"])


def test_the_log_form_takes_zones_an_interviewer_length_and_an_email(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    jid, cid = _job(store)
    c = local_client(store)
    r = c.post(f"/candidates/{cid}/log", data={
        "kind": "interview", "body": "System design", "at": "2026-11-10T12:00", "tz": "America/New_York",
        "job_id": str(jid), "interviewer": "Dana Lee", "interviewer_email": "dana@x.com",
        "interviewer_tz": "Europe/London", "candidate_tz": "America/New_York", "minutes": "60"},
        follow_redirects=False)
    assert r.status_code == 303
    iv = _ivs(store, cid)[0]
    assert iv["at"] == "2026-11-10T17:00:00+00:00"  # noon in New York, typed in that zone
    assert iv["body"] == "System design (60 min)" and iv["data"]["interviewer"] == "Dana Lee"
    assert iv["data"]["interviewer_tz"] == {"Dana Lee": "Europe/London"}
    assert store.candidate_zone(cid) == "America/New_York"
    book = store.interviewer_book()
    assert book["dana lee"]["email"] == "dana@x.com" and book["dana lee"]["time_zone"] == "Europe/London"
    assert "12:00 PM EST (9:00 AM PST)" in flash_of(r, "notice")
    ics = interviews.ics_for_interview(store, cid, iv["id"])
    text = "\n".join(_unfold(ics))
    assert "SUMMARY:System design: Ana Ray (Dana Lee)" in text
    assert "Dana Lee's time: Tue Nov 10, 5:00 PM GMT" in text.replace("\\,", ",").replace("\\n", "\n")
    bad = c.post(f"/candidates/{cid}/log", data={"kind": "interview", "body": "x", "at": "2026-11-11T10:00",
                                                 "interviewer": "Dana Lee", "interviewer_email": "nope"},
                 follow_redirects=False)
    assert "doesn't look like an email" in flash_of(bad, "error")
    assert len(_ivs(store, cid)) == 1


def test_a_typed_interviewer_email_is_saved_for_mailto_links(store):
    jid, cid = _job(store)
    store.log(cid, "interview", "Screen", at=(datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(),
              job_id=jid, interviewer="Priya Nair <priya@x.com>")
    other = store.add_candidate(name="Omar Ali")["id"]
    store.add_to_pipeline(other, jid, "Interviews")
    store.log(other, "interview", "Screen", at=(datetime.now(timezone.utc) - timedelta(hours=31)).isoformat(),
              job_id=jid, interviewer="Priya Nair")  # no email typed this time: the saved one is used
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "feedback_owed"]
    assert len(items) == 1 and items[0]["mailto"].startswith("mailto:priya@x.com?")
    w = interviews.week(store, interviews.week_start(
        actions.as_local(datetime.now(timezone.utc) - timedelta(hours=30)).date()))
    assert w["owed_people"][0]["email"] == "priya@x.com"


# ------------------------------------------------------------ .ics and calendar pages
def test_ics_titles_are_unique_per_session_and_the_description_has_no_stray_separator(store):
    reports.save_user_zone(store, "UTC")
    jid, cid = _job(store, "Grace Example")
    store.log(cid, "interview", "System design (45 min)", at="2027-02-10T15:00:00+00:00", job_id=jid,
              interviewer="Dana Lee")
    store.log(cid, "interview", "Coding", at="2027-02-10T16:00:00+00:00", job_id=jid, interviewer="Sam Roe")
    lines = _unfold(interviews.ics_for_month(store, date(2027, 2, 1)))
    summaries = [ln for ln in lines if ln.startswith("SUMMARY:")]
    assert summaries == ["SUMMARY:System design: Grace Example (Dana Lee)", "SUMMARY:Coding: Grace Example (Sam Roe)"]
    assert not any("; " in ln for ln in lines if ln.startswith("DESCRIPTION:"))


def test_the_week_ics_and_the_week_page_buttons_cover_the_weeks_months(store):
    reports.save_user_zone(store, "UTC")
    jid, cid = _job(store)
    store.log(cid, "interview", "Nov one", at="2026-11-30T15:00:00+00:00", job_id=jid)
    store.log(cid, "interview", "Dec one", at="2026-12-03T15:00:00+00:00", job_id=jid)
    store.log(cid, "interview", "Next week", at="2026-12-08T15:00:00+00:00", job_id=jid)
    c = local_client(store)
    r = c.get("/interviews.ics?week=2026-12-02")
    assert r.status_code == 200 and "interviews-week-2026-11-30.ics" in r.headers["content-disposition"]
    assert r.text.count("BEGIN:VEVENT") == 2 and "Next week" not in r.text
    assert c.get("/interviews.ics?week=garbage").status_code == 400
    html = c.get("/interviews?view=week&week=2026-11-30").text
    assert "Download this week" in html and "Download November 2026" in html and "Download December 2026" in html
    assert "/interviews.ics?month=2026-12" in html and "/interviews.ics?week=2026-11-30" in html


def test_a_clash_does_not_name_the_same_candidate_twice(store):
    reports.save_user_zone(store, "UTC")
    jid, cid = _job(store, "Kenji Example")
    store.log(cid, "interview", "CT interview", at="2027-02-10T15:00:00+00:00", job_id=jid, interviewer="Tia B.")
    store.log(cid, "interview", "Overlap clash", at="2027-02-10T15:15:00+00:00", job_id=jid, interviewer="Tia B.")
    w = interviews.week(store, date(2027, 2, 8))
    text = next(c["text"] for c in w["clashes"] if c["kind"] == "interviewer")
    assert text.count("Kenji Example") == 1 and "CT interview and Overlap clash" in text
    other = store.add_candidate(name="Lena Wu")["id"]
    store.log(other, "interview", "Screen", at="2027-02-10T15:00:00+00:00", job_id=jid, interviewer="Tia B.")
    w = interviews.week(store, date(2027, 2, 8))
    assert any("Kenji Example and Lena Wu" in c["text"] or "Lena Wu and Kenji Example" in c["text"] for c in w["clashes"])


def test_the_zone_banner_label_is_never_a_bare_offset():
    z = __import__("zoneinfo").ZoneInfo("Pacific/Kiritimati")
    assert interviews.zone_label(datetime(2026, 11, 10, tzinfo=timezone.utc).astimezone(z)) == "UTC+14"


# ------------------------------------------------------------ Decide
def test_booking_a_loop_does_not_make_someone_in_an_early_stage_a_finalist(store):
    job = store.add_job("Staff Engineer", "Acme")
    early = store.add_candidate(name="Arjun Early")["id"]
    late = store.add_candidate(name="Lia Late")["id"]
    held = store.add_candidate(name="Hal Held")["id"]
    store.add_to_pipeline(early, job["id"], "Sourced")
    store.add_to_pipeline(late, job["id"], "Interviewing")
    store.add_to_pipeline(held, job["id"], "Screening")
    soon = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    store.log(early, "interview", "Onsite", at=soon, job_id=job["id"], interviewer="Dana")
    store.log(held, "interview", "Phone screen", at=ago, job_id=job["id"], interviewer="Dana")
    names = [p["name"] for p in decide.finalists(store, job["id"])["people"]]
    assert "Arjun Early" not in names and names == ["Lia Late", "Hal Held"]


# ------------------------------------------------------------ Today's feedback cards
def test_feedback_cards_merge_per_candidate(store):
    jid, cid = _job(store, "Kenji Example")
    ago = lambda h: (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()  # noqa: E731
    store.log(cid, "interview", "Screen one", at=ago(30), job_id=jid, interviewer="Dana Lee, Priya Nair")
    store.log(cid, "interview", "Screen two", at=ago(29), job_id=jid, interviewer="Sam Roe")
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "feedback_owed"]
    assert len(items) == 1
    card = items[0]
    assert card["title"] == "Kenji Example is waiting on feedback from Dana Lee, Priya Nair and Sam Roe"
    assert {p["name"] for p in card["people"]} == {"Dana Lee", "Priya Nair", "Sam Roe"}
    assert all(p["mailto"].startswith("mailto:") for p in card["people"])
    page = local_client(store).get("/").text
    assert "waiting on feedback from Dana Lee, Priya Nair and Sam Roe" in page and page.count("Copy request") == 3


# ------------------------------------------------------------ holidays
def test_us_holidays_are_built_in():
    assert loops.us_holiday(date(2026, 11, 26)) == "Thanksgiving"
    assert loops.us_holiday(date(2026, 11, 27)) == "the day after Thanksgiving"
    assert loops.us_holiday(date(2026, 12, 25)) == "Christmas Day"
    assert loops.us_holiday(date(2027, 7, 5)) == "Independence Day"  # July 4 is a Sunday: taken on Monday
    assert loops.us_holiday(date(2026, 9, 7)) == "Labor Day" and loops.us_holiday(date(2026, 5, 25)) == "Memorial Day"
    assert loops.us_holiday(date(2026, 11, 25)) is None


def test_plan_loop_skips_thanksgiving_and_says_so(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    jid, cid = _job(store)
    loops.save_template(store, jid, "System design | 60 | Dana Lee", "9-17")
    p = loops.plan(store, jid, cid, start="2026-11-26", now=datetime(2026, 11, 5, tzinfo=timezone.utc))
    day = actions.as_local(datetime.fromisoformat(p["sessions"][0]["at"])).date()
    assert day == date(2026, 11, 30)  # Thursday and the day after are skipped, then the weekend
    assert any("Skipped Thanksgiving (Thu Nov 26)" in w for w in p["warnings"])


def _ivs(store, cid):
    return [t for t in store.timeline(cid) if t["kind"] == "interview"]
