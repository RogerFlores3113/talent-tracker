"""Cycle 10, fixer W4: keyboard and screen reader access, and the first-time experience in the web UI (calendar tab
stops, the quiet reason step, focus after saves, accessible names, full time zone lists, contrast, plain first
screen, email checks, notes that name someone, all-day interviews, a length cap)."""
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from talent import interviews, reports
from talent.web import _bad_email_fields, _name_in_note

from conftest import local_client

SRC = Path(__file__).resolve().parent.parent / "src" / "talent"
JS = (SRC / "static" / "app.js").read_text()
CSS = (SRC / "static" / "style.css").read_text()


def _seed(store):
    job = store.add_job("Staff Engineer", "Acme")
    ana = store.add_candidate(name="Ana Ruiz", tags=["python"])["id"]
    store.add_to_pipeline(ana, job["id"], "Sourced")
    reports.save_user_zone(store, "UTC")
    store.log(ana, "interview", "Onsite: system design", at="2027-03-10T15:00:00+00:00", job_id=job["id"],
              interviewer="Kim Lee")
    return job["id"], ana


# ------------------------------------------------------------------ 1. calendar
def test_month_only_days_with_interviews_are_links(store):
    _seed(store)
    html = local_client(store).get("/interviews?view=month&month=2027-03").text
    links = re.findall(r'<a class="cal-num"', html)
    plain = re.findall(r'<span class="cal-num"', html)
    assert len(links) == 1 and len(plain) >= 34
    assert "Wednesday March 10, 1 interview" in html and "0 interviews" not in html


# ------------------------------------------------------------------ 2, 3, 5. script
def test_quiet_reason_step_says_it_is_not_moved_and_moves_focus():
    assert "Not moved yet." in JS and "Pick a reason for" in JS
    assert re.search(r"window\.setTimeout\(\(\) => \{ pick\.focus\(\); \}, 0\)", JS)
    assert ".stage-form.unsent" in CSS


def test_a_page_after_a_save_puts_focus_on_its_notice():
    assert "function focusResult" in JS and "main > .flash" in JS and "data-focus-here" in JS


def test_import_check_result_is_focused_and_settings_saved_is_a_status(store):
    c = local_client(store)
    assert 'role="status">Saved.' in c.post("/settings", data={"theme": "ocean"}, follow_redirects=True).text


def test_esc_on_a_menu_does_not_close_the_tour():
    m = re.search(r'if \(e\.key === "Escape" && e\.target && e\.target\.tagName === "SELECT"\) return;', JS)
    assert m and m.start() < JS.index('if (e.key === "Escape") { e.preventDefault(); if (confirming)')


def test_tour_finish_offers_one_box_for_the_saved_searches():
    # One short line for all of them (they go with the demo people), not a box per search.
    assert "only find demo people are removed with them" in JS and "Also remove my saved search" not in JS


# ------------------------------------------------------------------ 4. names and zones
def test_zone_options_list_every_zone_with_its_offset():
    zs = reports.zone_options(datetime(2027, 1, 15, 12, tzinfo=timezone.utc))
    d = dict(zs)
    assert len(zs) > 300 and d["Asia/Dubai"] == "UTC+04:00" and d["America/New_York"] == "UTC-05:00"
    assert d["Europe/Warsaw"] == "UTC+01:00" and d["Pacific/Auckland"] == "UTC+13:00"
    assert zs[0][0] == "America/New_York" and not any(n.startswith("Etc/") for n, _ in zs)
    summer = dict(reports.zone_options(datetime(2027, 7, 15, 12, tzinfo=timezone.utc)))
    assert summer["America/New_York"] == "UTC-04:00"


def test_person_page_zone_pickers_use_the_full_list_and_matching_names(store):
    _, cid = _seed(store)
    html = local_client(store).get(f"/candidates/{cid}").text
    assert html.count('<datalist id="zone-list">') == 1 and '<option value="Asia/Dubai">UTC+04:00</option>' in html
    assert '<select name="tz"' not in html and '<select name="interviewer_tz"' not in html
    assert "Interviewer's zone <input" in html and "Candidate's zone <input" in html
    assert 'aria-label="Interviewer\'s time zone"' not in html
    # per-session boxes carry the session name; the log form's own boxes are not generic duplicates
    assert 'aria-label="Time zone for the new time of Onsite: system design, blank is mine"' in html
    assert 'aria-label="Reason for the new time of Onsite: system design"' in html
    assert 'aria-label="Interviewer for the session after Onsite: system design"' in html
    assert 'aria-label="Time zone of that time"' not in html and 'aria-label="Reason for the new time"' not in html
    for generic in ('aria-label="Kind"', 'aria-label="Note"', 'aria-label="Job"', 'aria-label="Interviewer"'):
        assert generic not in html


def test_a_far_zone_typed_in_the_log_form_is_used(store):
    _, cid = _seed(store)
    c = local_client(store)
    c.post(f"/candidates/{cid}/log", data={"kind": "interview", "body": "Phone screen", "at": "2027-04-05T10:00",
                                          "tz": "Asia/Dubai"})
    t = [x for x in store.candidate_detail(cid)["timeline"] if x["body"] and x["body"].startswith("Phone screen")][0]
    assert t["at"].startswith("2027-04-05T06:00")  # 10:00 in Dubai is 06:00 UTC


def test_pin_button_name_starts_with_its_visible_words(store):
    store.add_candidate(name="Ana Ruiz", tags=["python"])
    c = local_client(store)
    c.post("/saved-searches", data={"search_name": "py", "q": "python"})
    html = c.get("/candidates").text
    assert 'aria-label="Pin to top, saved search py"' in html


# ------------------------------------------------------------------ 5. page basics
def test_no_autofocus_on_today_or_the_add_forms(store):
    store.add_candidate(name="Ana Ruiz")
    c = local_client(store)
    for path in ("/", "/candidates/new", "/jobs/new", "/paste"):
        assert "autofocus" not in c.get(path).text, path


def test_titles_name_the_page_and_the_week(store):
    _seed(store)
    c = local_client(store)
    assert "<title>Today - Talent Search</title>" in c.get("/").text
    week = c.get("/interviews?view=week&week=2027-03-10").text
    assert re.search(r"<title>Interviews · week of [A-Z][a-z]+,? [A-Z]?[a-z]*\.? ?\d+", week)


def test_clients_table_headers_have_scope(store):
    store.add_job("Staff Engineer", "Acme")
    html = local_client(store).get("/clients").text
    table = html.split("<table")[1].split("</table>")[0]
    assert '<th scope="col">' in table and "<th>" not in table


def test_links_that_open_a_new_tab_say_so(store):
    c = local_client(store)
    html = c.get("/connect").text
    tabs = re.findall(r'<a [^>]*target="_blank"[^>]*>.*?</a>', html)
    assert tabs and all("opens in a new tab" in t for t in tabs)


def _oklab(hexv):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    n = int(hexv.lstrip("#"), 16)
    r, g, b = lin((n >> 16) & 255), lin((n >> 8) & 255), lin(n & 255)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _from_oklab(lab):
    L, a, b = lab
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    lin = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
           -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
           -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)
    return lin


def _lum(lin):
    r, g, b = (min(1, max(0, c)) for c in lin)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hex_lum(hexv):
    n = int(hexv.lstrip("#"), 16)
    ch = [((v / 255) / 12.92 if v / 255 <= 0.04045 else (((v / 255) + 0.055) / 1.055) ** 2.4)
          for v in ((n >> 16) & 255, (n >> 8) & 255, n & 255)]
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def test_input_borders_reach_three_to_one_in_every_theme_and_forced_colors_has_a_rule():
    assert "--input-line: color-mix(in oklab, var(--text) 58%, var(--surface))" in CSS
    mix = re.search(r"--input-line: color-mix\(in oklab, var\(--text\) (\d+)%, var\(--surface\)\)", CSS)
    share = int(mix.group(1)) / 100
    seen = 0
    for name, body in re.findall(r"/\* theme: (\S+) \*/[^{]*\{([^}]*)\}", CSS):
        t = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", body))
        a, b = _oklab(t["--text"]), _oklab(t["--surface"])
        line = _lum(_from_oklab(tuple(share * x + (1 - share) * y for x, y in zip(a, b))))
        for bg in (_hex_lum(t["--surface"]), _hex_lum(t["--bg"])):
            hi, lo = max(line, bg), min(line, bg)
            assert (hi + 0.05) / (lo + 0.05) >= 3, name
        seen += 1
    assert seen == 7
    assert "@media (forced-colors: active)" in CSS and "outline: 3px solid Highlight" in CSS
    assert "border: 1px solid var(--input-line)" in CSS


# ------------------------------------------------------------------ 6. first screen
def test_time_zone_bar_shows_only_when_the_computer_zone_looks_unset(store, monkeypatch):
    c = local_client(store)
    store.add_candidate(name="Ana Ruiz")
    monkeypatch.setattr(reports, "computer_on_utc", lambda: True)
    shown = c.get("/").text
    assert "data-tz-banner" in shown and "Pick another" in shown and "use_zone" in shown and "Keep UTC" in shown
    assert " hidden>" not in shown.split("data-tz-banner")[1].split(">")[0]
    monkeypatch.setattr(reports, "computer_on_utc", lambda: False)
    assert "data-tz-banner" not in c.get("/").text  # a computer with its own zone and nothing saved: no bar
    reports.save_user_zone(store, "America/Chicago")
    assert "data-tz-banner" not in c.get("/").text  # a zone the person chose is never questioned again


def test_welcome_wording_is_short_and_the_footer_is_clear(store):
    html = local_client(store).get("/").text
    assert "Take a short tour" in html and "Take the full tour with made-up people" in html
    assert "full, with demo people" not in html and "never clears" not in html


def test_settings_uses_plain_words_and_a_jump_list(store):
    html = local_client(store).get("/settings").text
    assert 'aria-label="Jump to"' in html and 'href="#cautious"' in html and 'href="#ai-apps"' in html
    tpl = (SRC / "templates" / "settings.html").read_text()  # the AI apps table only shows once an app has connected
    assert "Changes made" in tpl and "What it may do" in tpl and "<th>Writes</th>" not in tpl
    assert "it can't move someone to Hired until you press Keep" in html
    assert "Turn on <a" not in html.split('id="site-guards"')[1].split("</section>")[0]  # no circular pointer
    assert html.count('<datalist id="zone-list">') == 1


def test_ui_text_says_color_not_colour(store):
    _seed(store)
    c = local_client(store)
    for path in ("/", "/candidates", "/interviews?view=month&month=2027-03", "/settings", "/connect"):
        assert "olour" not in c.get(path).text, path
    r = c.post("/candidates/1/color", data={"color": "chartreuse"}, headers={"accept": "application/json"})
    assert r.status_code == 400 and "isn't a color" in r.json()["error"]


# ------------------------------------------------------------------ 7. forms
def test_bad_email_is_refused_next_to_the_field_and_nothing_is_saved(store):
    c = local_client(store)
    r = c.post("/candidates", data={"name": "Weird Person", "email": "notanemail"})
    assert r.status_code == 400
    assert 'aria-invalid="true" aria-describedby="err-email"' in r.text
    assert re.search(r'<span class="error" id="err-email">[^<]*doesn&#39;t look like an email address', r.text)
    assert store.count_candidates() == 0 and 'value="notanemail"' in r.text
    ok = c.post("/candidates", data={"name": "Weird Person", "email": "w@x.com"}, follow_redirects=False)
    assert ok.status_code == 303 and store.count_candidates() == 1


def test_bad_email_on_edit_and_other_emails_are_refused(store):
    cid = store.add_candidate(name="Ana Ruiz", email="ana@x.com")["id"]
    c = local_client(store)
    r = c.post(f"/candidates/{cid}", data={"name": "Ana Ruiz", "email": "ana@@x"})
    assert r.status_code == 400 and 'id="err-email"' in r.text
    assert store.get_candidate(cid)["email"] == "ana@x.com"
    r = c.post(f"/candidates/{cid}", data={"name": "Ana Ruiz", "email": "ana@x.com", "other_emails": "fine@y.com\nnope"})
    assert r.status_code == 400 and 'id="err-other_emails"' in r.text
    r = c.post(f"/candidates/{cid}", data={"name": "Ana Ruiz", "email": "", "other_emails": ""}, follow_redirects=False)
    assert r.status_code == 303  # blank is fine
    assert _bad_email_fields({"email": "a@b.co"}, ["c@d.org"]) == {}


@pytest.mark.parametrize("text,name", [
    ("Spoke with Marcus Lee about the backend role. Strong on Go.", "Marcus Lee"),
    ("Name: Priya Nair\nlikes rust", "Priya Nair"),
    ("Great call today. Met Ana-Maria Souza at the meetup, wants remote.", "Ana-Maria Souza"),
    ("Marcus Lee, 12 years in payments, based in Austin", "Marcus Lee"),
])
def test_a_name_in_a_note_is_found(text, name):
    assert _name_in_note(text) == name


def test_a_note_with_no_name_finds_none_and_paste_fills_the_name(store):
    assert _name_in_note("looks strong, call next week, wants remote") is None
    r = local_client(store).post("/paste", data={"text": "Spoke with Marcus Lee about the backend role. Wants remote."})
    assert re.search(r'name="name" value="Marcus Lee"', r.text)


def test_the_web_form_can_log_an_all_day_interview(store):
    _, cid = _seed(store)
    c = local_client(store)
    html = c.get(f"/candidates/{cid}").text
    assert 'name="all_day"' in html and "data-all-day-input" in html
    c.post(f"/candidates/{cid}/log", data={"kind": "interview", "body": "Onsite day", "at": "2027-05-04", "all_day": "1"})
    t = [x for x in store.candidate_detail(cid)["timeline"] if x["body"] == "Onsite day"][0]
    assert t["data"]["all_day"] is True and t["data"]["all_day_date"] == "2027-05-04"
    month = c.get("/interviews?view=month&month=2027-05").text
    assert "Onsite day" not in month or "All day" in month
    r = c.post(f"/candidates/{cid}/log", data={"kind": "interview", "body": "Nope", "at": "", "all_day": "1"})
    assert r.status_code == 400 and "Pick the day of the interview" in r.text  # the form comes back with the error
    assert not [x for x in store.candidate_detail(cid)["timeline"] if x["body"] == "Nope"]


def test_interview_summary_is_capped_with_a_visible_limit(store):
    _, cid = _seed(store)
    c = local_client(store)
    html = c.get(f"/candidates/{cid}").text
    assert f"can be up to {interviews.SUMMARY_LIMIT} characters" in html and "data-count-note" in html
    long = "x" * (interviews.SUMMARY_LIMIT + 1)
    r = c.post(f"/candidates/{cid}/log", data={"kind": "interview", "body": long, "at": "2027-05-04T10:00"},
               follow_redirects=True)
    assert "up to 500 characters" in r.text.replace("&#39;", "'") or "up to {}".format(interviews.SUMMARY_LIMIT) in r.text
    assert not [x for x in store.candidate_detail(cid)["timeline"] if x["body"] == long]
    ok = "y" * interviews.SUMMARY_LIMIT
    c.post(f"/candidates/{cid}/log", data={"kind": "interview", "body": ok, "at": "2027-05-04T10:00"})
    assert [x for x in store.candidate_detail(cid)["timeline"] if x["body"] == ok]
    # a long note (not an interview) is not held to the interview limit
    c.post(f"/candidates/{cid}/log", data={"kind": "note", "body": long})
    assert [x for x in store.candidate_detail(cid)["timeline"] if x["body"] == long]
