"""c12: tour highlight targets (saved searches, a candidate's page, interviews) and the tall-target scroll."""
from __future__ import annotations

import re
from pathlib import Path

from talent import tour
from talent.sample_data import load_sample_data

ROOT = Path(__file__).resolve().parent.parent


def _step(sid):
    return next(s for s in tour.STEPS if s["id"] == sid)


def test_person_step_frames_the_summary_not_just_the_name(store, client):
    s = _step("person")
    assert s["target"][0] == "main .profile-summary"
    assert "summary" in s["body"]
    load_sample_data(store)
    page = client.get("/candidates/1").text
    box = page[page.index('class="profile-summary"'):]
    # the summary holds the name, the skills, the contact line and the facts, and ends before the work history
    head = box[:box.index("Work history") if "Work history" in box else len(box)]
    for part in ("<h1>", 'class="chips"', 'class="contact-line"', 'class="facts"'):
        assert part in head, part


def test_saved_searches_area_is_never_an_empty_bar(store, client):
    load_sample_data(store)
    page = client.get("/candidates").text
    bar = page[page.index('id="saved-searches"'):]
    bar = bar[:bar.index("</div>")]
    assert "Saved searches: none yet." in bar
    assert "Run a search or pick a filter" in bar
    # with a search on, the hint gives way to the Save this search control
    page = client.get("/candidates", params={"q": "python"}).text
    bar = page[page.index('id="saved-searches"'):]
    assert "Save this search" in bar and "Run a search or pick a filter" not in bar[:bar.index("</details>")]


def test_interviews_step_shows_the_heading_and_the_calendar():
    s = _step("interviews")
    assert s["target"][0] == "main .sec-head" and s["reach"] == "#interviews-view"


def test_tall_targets_open_at_their_top():
    js = (ROOT / "src" / "talent" / "static" / "app.js").read_text()
    assert "window.innerHeight * 0.7" in js
    assert re.search(r"Math\.min\(tb\.top, rb\.top\)", js)


def test_add_todo_row_wraps():
    css = (ROOT / "src" / "talent" / "static" / "style.css").read_text()
    assert ".add-todo { display: flex; flex-wrap: wrap;" in css
