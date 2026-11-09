"""Cycle 10, fixer A: keyboard and screen reader access (skip link, current-page marks, board stage menu,
calendar and table names, heading order, muted-text contrast)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from talent import reports
from talent.web import color_info

from conftest import local_client

STATIC = Path(__file__).resolve().parent.parent / "src" / "talent" / "static"


def _seed(store):
    job = store.add_job("Staff Engineer", "Acme")
    ana = store.add_candidate(name="Ana Ruiz", tags=["python"])["id"]
    store.add_to_pipeline(ana, job["id"], "Sourced")
    reports.save_user_zone(store, "UTC")
    store.log(ana, "interview", "Onsite: system design", at="2027-03-10T15:00:00+00:00", job_id=job["id"],
              interviewer="Kim Lee")
    return job["id"], ana


def _headings(html):
    return [int(n) for n in re.findall(r"<h([1-6])[ >]", html)]


def _no_jumps(html):
    hs = _headings(html)
    assert hs and hs[0] == 1, hs
    for a, b in zip(hs, hs[1:]):
        assert b <= a + 1, hs


# ---------------------------------------------------------------- skip link, current page
def test_skip_link_and_main_target_on_every_page(store):
    jid, cid = _seed(store)
    c = local_client(store)
    for path in ("/", "/candidates", f"/candidates/{cid}", "/jobs", f"/jobs/{jid}", "/interviews", "/settings"):
        html = c.get(path).text
        assert '<a class="skip-link" href="#main">Skip to main content</a>' in html, path
        assert '<main id="main" tabindex="-1">' in html, path
        assert html.index("skip-link") < html.index("<header"), path + " skip link must be the first stop"


def test_main_nav_marks_the_current_page(store):
    _seed(store)
    c = local_client(store)
    html = c.get("/").text
    assert re.search(r'<a href="/" class="on" aria-current="page">Today</a>', html)
    assert html.count('aria-current="page"') == 1
    html = c.get("/jobs").text
    assert re.search(r'<a href="/jobs" class="on" aria-current="page">Jobs</a>', html)
    assert 'aria-current="page">Today' not in html


def test_class_on_always_comes_with_aria_current(store):
    jid, cid = _seed(store)
    c = local_client(store)
    for path in ("/", "/jobs", "/jobs?status=open", f"/jobs/{jid}", f"/jobs/{jid}?view=table",
                 f"/jobs/{jid}/shortlist", f"/jobs/{jid}/outreach", f"/jobs/{jid}/decide", "/interviews",
                 "/interviews?view=month&month=2027-03", "/settings"):
        html = c.get(path).text
        for tag in re.findall(r"<a [^>]*class=\"on\"[^>]*>", html):
            assert 'aria-current="page"' in tag, (path, tag)
        assert 'aria-current="page"' in html, path


def test_job_tabs_and_view_toggle_and_status_filter(store):
    jid, _ = _seed(store)
    c = local_client(store)
    html = c.get(f"/jobs/{jid}").text
    assert re.search(r'class="on" aria-current="page">Pipeline</a>', html)
    assert re.search(r'class="on" aria-current="page">Board</a>', html)
    assert not re.search(r'aria-current="page">Table</a>', html)
    html = c.get(f"/jobs/{jid}?view=table").text
    assert re.search(r'class="on" aria-current="page">Table</a>', html)
    html = c.get("/jobs?status=open").text
    assert re.search(r'class="on" aria-current="page">Open</a>', html) and not re.search(r'aria-current="page">All<', html)


# ---------------------------------------------------------------- the board
def test_board_columns_are_regions_with_lists_and_a_hint(store):
    jid, ana = _seed(store)
    html = local_client(store).get(f"/jobs/{jid}").text
    assert '<div class="board">' in html and 'class="board" role="list"' not in html
    assert 'role="listitem" aria-label=' not in html
    assert re.search(r'<section class="col o-open" data-stage="Sourced" aria-labelledby="stage-1">', html)
    assert '<h3 id="stage-1"><span>Sourced</span><span class="sr-only">, </span><span class="muted">1</span></h3>' in html
    assert 'role="list" aria-label="People in Sourced"' in html and 'class="pc" role="listitem"' in html
    assert "use the Stage menu on their card" in html
    assert '<h2 class="sr-only">Pipeline by stage</h2>' in html


def test_job_pages_have_no_heading_jumps(store):
    jid, _ = _seed(store)
    c = local_client(store)
    for path in (f"/jobs/{jid}", f"/jobs/{jid}?view=table", "/interviews", "/interviews?view=month&month=2027-03", "/"):
        _no_jumps(c.get(path).text)
    # a person's page: the name comes first, "Same person?" after it
    a = store.add_candidate(name="Ana Ruiz", email="ana@example.com")["id"]
    store.add_candidate(name="Ana Ruiz", email="ana@other.example")
    html = c.get(f"/candidates/{a}").text
    if "Same person?" in html:
        assert html.index("<h1>") < html.index("Same person?")
    _no_jumps(html)


def test_job_table_has_a_caption_and_column_scopes(store):
    jid, _ = _seed(store)
    html = local_client(store).get(f"/jobs/{jid}?view=table").text
    assert "<caption class=\"sr-only\">" in html and '<th scope="col">Stage</th>' in html
    assert 'scope="rowgroup"' in html


def test_today_has_a_hidden_h1_and_one_search_status_line(store):
    _seed(store)
    html = local_client(store).get("/").text
    assert '<h1 class="sr-only">Today</h1>' in html
    assert '<p class="sr-only" id="results-status" role="status"></p>' in html
    assert '<section id="results" aria-live' not in html


def test_welcome_page_still_has_exactly_one_h1(store):
    html = local_client(store).get("/").text
    assert _headings(html).count(1) == 1


# ---------------------------------------------------------------- month and week
def test_month_day_links_have_distinct_names_and_weeks_are_named(store):
    _seed(store)
    html = local_client(store).get("/interviews?view=month&month=2027-03").text
    assert "Wednesday March 10, 1 interview, week of Mar 8</span>" in html
    assert "0 interviews" not in html  # a day with nothing booked is plain text, not a link (c10 w4)
    names = re.findall(r'<a class="cal-num"[^>]*>.*?<span class="cal-vh">([^<]*)</span></a>', html)
    assert names and len(set(names)) == len(names)
    assert re.findall(r'<ul class="cal-week" aria-label="Week of [A-Z][a-z]{2} \d+">', html)
    assert 'role="group" aria-label="Interviews in' in html and 'role="group" aria-label="Color key"' in html


def test_today_link_marks_the_current_month_and_week_marks_today(store):
    _seed(store)
    c = local_client(store)
    html = c.get("/interviews?view=month").text
    assert re.search(r'href="/interviews\?view=month" aria-current="page">Today</a>', html)
    from talent import interviews
    days = interviews.week(store)["days"]
    assert [d["today"] for d in days].count(True) <= 1
    html = c.get("/interviews?view=week").text
    if any(d["today"] for d in days):  # a weekend day only shows when something is booked on it
        assert 'class="day" aria-current="date"' in html and "(today)</span></h3>" in html


# ---------------------------------------------------------------- candidates table
def test_candidates_table_caption_scope_and_sort(store):
    _seed(store)
    c = local_client(store)
    html = c.get("/candidates?sort=name&dir=asc").text
    assert '<caption class="sr-only">Candidates, sorted by name, ascending</caption>' in html
    assert html.count('<th scope="col"') == 10  # nine before, plus the tick-a-person column (cycle 10 W3)
    assert re.search(r'<th scope="col" aria-sort="ascending"><a [^>]*>Name <span aria-hidden="true">▲</span></a></th>', html)
    assert html.count("aria-sort=") == 1
    html = c.get("/candidates?sort=updated&dir=desc").text
    assert re.search(r'<th scope="col" aria-sort="descending"><a [^>]*>Updated ', html)
    html = c.get("/candidates?q=ana").text
    assert "best match first" in html and "aria-sort=" not in html
    assert 'role="group" aria-label="Filters that are on"' in c.get("/candidates?q=ana&tag=python").text


def test_color_bubble_name_is_short_and_the_how_to_is_in_the_tooltip(store):
    i = color_info("red", "Ana Ruiz")
    assert i["aria"] == "Ana Ruiz: color red. Click for orange."
    assert "Shift+Enter" in i["title"] and "Shift+Enter" not in i["aria"]


def test_interview_forms_on_a_person_page_name_their_session(store):
    jid, cid = _seed(store)
    html = local_client(store).get(f"/candidates/{cid}").text
    assert 'aria-label="Interviewer, Onsite: system design"' in html
    assert 'aria-label="Feedback on Onsite: system design"' in html
    assert 'aria-label="Next session after Onsite: system design"' in html
    # only the new-entry form's own Interviewer box keeps the short name; every per-session box names its session
    assert html.count('aria-label="Interviewer (names separated by commas) for a new interview"') == 1
    assert 'aria-label="Session name"' not in html


# ---------------------------------------------------------------- app.js and css
def _js():
    return (STATIC / "app.js").read_text()


def test_stage_menus_wait_for_enter_or_blur_like_the_filters():
    js = _js()
    assert 'const DEFER = "select[data-autosubmit], select[data-stage-pick], select[data-table-stage], form.stage-form .reason select";' in js
    # the arrow-key hold and the Enter / blur commit run for every menu in DEFER
    assert 'if (el.dataset.kb) { el.dataset.pending = "1"; return; }' in js
    assert 'if (e.key === "Enter") {' in js and 'document.addEventListener("focusout"' in js
    # the old immediate-submit on every change is gone
    assert "// Stage menus: an out stage shows the optional reason first; any other pick (or a reason) moves at once." not in js


def test_board_move_is_announced_and_focus_comes_back():
    js = _js()
    assert 'document.addEventListener("htmx:beforeRequest"' in js and 'document.addEventListener("htmx:afterSettle"' in js
    assert '" moved to "' in js and "select[data-stage-pick]" in js
    assert 'role", "status"' in js  # the live region the move is spoken in
    assert "tr.more, li.more" in js and 'id="results-status"' not in js and 'getElementById("results-status")' in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_still_parses():
    r = subprocess.run(["node", "--check", str(STATIC / "app.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _lum(h):
    h = h.lstrip("#")
    ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    ch = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def _ratio(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_rejected_and_withdrawn_columns_keep_muted_text_readable_in_every_theme():
    css = (STATIC / "style.css").read_text()
    assert ".col.o-lost { opacity" not in css and ".col.o-lost { border-style: dashed; }" in css
    themes = re.findall(r"--surface: (#\w+); --surface-2: (#\w+); --text: #\w+; --muted: (#\w+);", css)
    assert len(themes) >= 7
    for surface, surface2, muted in themes:
        assert _ratio(muted, surface2) >= 4.5, (muted, surface2)  # the column's own background
        assert _ratio(muted, surface) >= 4.5, (muted, surface)  # the cards inside it


def test_skip_link_is_hidden_until_focused():
    css = (STATIC / "style.css").read_text()
    assert ".skip-link { position: absolute;" in css and ".skip-link:focus { top:" in css
