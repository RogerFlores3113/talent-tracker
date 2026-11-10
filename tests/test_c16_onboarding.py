"""Cycle 16: first run and understanding features (time zone suggestion, locked page, folded explanations,
bulk bar, tour wording)."""
import re
from pathlib import Path

from talent import reports, tour

from conftest import local_client

SRC = Path(__file__).resolve().parent.parent / "src" / "talent"
JS = (SRC / "static" / "app.js").read_text()


def _utc(monkeypatch):
    monkeypatch.setattr(reports, "computer_on_utc", lambda: True)


def test_zone_banner_is_a_one_click_button_posting_the_detected_zone(store, monkeypatch):
    _utc(monkeypatch)
    c = local_client(store)
    store.add_candidate(name="Ana Ruiz")
    html = c.get("/").text
    assert "data-tz-use" in html and 'name="use_zone"' in html and "Pick another" in html
    assert 'Intl.DateTimeFormat().resolvedOptions().timeZone' in JS and 'b.textContent = "Use " + zone' in JS
    r = c.post("/settings/time-zone", data={"next": "/", "use_zone": "America/Chicago", "time_zone": ""},
               follow_redirects=False)
    assert r.status_code == 303 and reports.user_zone(store) == "America/Chicago"
    # the typed box still works for another zone
    c.post("/settings/time-zone", data={"next": "/", "time_zone": "PT"}, follow_redirects=False)
    assert reports.user_zone(store) == "America/Los_Angeles"


def test_zone_banner_sits_before_the_welcome_cards_on_first_run(store, monkeypatch):
    _utc(monkeypatch)
    html = local_client(store).get("/").text
    assert html.index("data-tz-banner") < html.index("Welcome to Talent Search")
    assert html.count("data-tz-banner") == 1
    store.add_candidate(name="Ana Ruiz")
    later = local_client(store).get("/").text
    assert "data-tz-banner" in later


def test_zone_fallback_says_so_on_today(store, monkeypatch):
    _utc(monkeypatch)
    monkeypatch.setattr(reports, "fallback_zone", lambda s: "America/New_York")
    store.add_candidate(name="Ana Ruiz")
    text = re.sub(r"<[^>]+>", "", local_client(store).get("/").text)
    assert "Times show in America/New_York, your company's time zone, because this computer's clock is on UTC" in text


def test_locked_page_has_no_command_to_run(store):
    r = local_client(store, unlocked=False).get("/")
    assert r.status_code == 401 and "talent open" not in r.text
    assert "app icon" in r.text and "Applications" in r.text and "Start menu" in r.text
    assert "On Linux, open Talent Search from your applications menu, or run talent-search in a terminal." in r.text
    assert "—" not in r.text


def test_candidates_explanations_are_folded_under_one_details(store):
    for i in range(3):
        store.add_candidate(name=f"Ana {i}", tags=["python"])
    html = local_client(store).get("/candidates", params={"q": "python OR go", "hl": "python"}).text
    d = html[html.index('<details class="how-details"'):]
    d = d[:d.index("</details>")]
    assert "How does this work?" in d and "People with more of these skills come first" in d
    # the plain-language reading stays on the visible line, not inside the fold
    assert "How we read your search" not in d
    before = html[:html.index('<details class="how-details"')]
    assert "How we read your search" in before and "People with more of these skills" not in before


def test_candidates_empty_state_has_next_steps(store):
    html = local_client(store).get("/candidates").text
    assert 'id="cand-empty"' in html and "/import" in html and "/connect" in html


def test_settings_theme_note_says_claude_need_not_restart(store):
    html = local_client(store).get("/settings").text
    assert "You don't need to quit Claude" in html


def test_bulk_bar_folds_until_someone_is_ticked(store):
    store.add_candidate(name="Ana Ruiz")
    html = local_client(store).get("/candidates").text
    assert html.count("data-needs-ticks") >= 2 and "data-when-empty" in html
    assert 'form.querySelectorAll("[data-needs-ticks]")' in JS


def test_tour_text_uses_the_connect_page_button_names():
    body = {s["id"]: s["body"] for s in tour.STEPS}
    assert "Connect Claude" in body["claude"] and "Add skills" in body["skills"]
    assert "technical" not in body["codex"]


def test_import_result_has_no_command_line_hint(store):
    html = (SRC / "templates" / "import.html").read_text()
    assert "talent sync" not in html
