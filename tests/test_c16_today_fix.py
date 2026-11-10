"""Review fixes: Today time zone prompt, next steps, locked page, tour wording, Candidates line, job header, footer."""
from pathlib import Path

from talent import integrations, reports, tour

from conftest import local_client

SRC = Path(__file__).resolve().parent.parent / "src" / "talent"
JS = (SRC / "static" / "app.js").read_text()


def _utc(monkeypatch):
    monkeypatch.setattr(reports, "computer_on_utc", lambda: True)


def test_chosen_zone_is_never_questioned_again(store, monkeypatch):
    _utc(monkeypatch)
    store.add_candidate(name="Ana Ruiz")
    c = local_client(store)
    assert "data-tz-banner" in c.get("/").text
    c.post("/settings/time-zone", data={"time_zone": "America/Los_Angeles"}, follow_redirects=False)
    html = c.get("/").text
    assert "data-tz-banner" not in html and "time zone changed" not in html and "Pick another" not in html
    assert "checkZoneBanner" not in JS


def test_keep_choice_ends_the_prompt_and_button_has_no_question_mark(store, monkeypatch):
    _utc(monkeypatch)
    store.add_candidate(name="Ana Ruiz")
    c = local_client(store)
    html = c.get("/").text
    assert "Keep UTC" in html and 'value="UTC"' in html
    assert '"Use " + zone + "?"' not in JS and 'b.textContent = "Use " + zone;' in JS
    assert "America/Chicago, or ET" not in html
    c.post("/settings/time-zone", data={"next": "/", "use_zone": "UTC"}, follow_redirects=False)
    assert "data-tz-banner" not in c.get("/").text


def test_keep_names_the_company_zone_when_that_is_what_shows(store, monkeypatch):
    _utc(monkeypatch)
    monkeypatch.setattr(reports, "fallback_zone", lambda s: "America/New_York")
    assert "Keep America/New_York" in local_client(store).get("/").text


def test_banner_comes_before_the_welcome_cards(store, monkeypatch):
    _utc(monkeypatch)
    html = local_client(store).get("/").text
    assert html.index("data-tz-banner") < html.index("Welcome to Talent Search")


def test_today_next_steps_when_nothing_needs_you(store, monkeypatch):
    monkeypatch.setattr(integrations, "claude_connected", lambda path=None: False)
    store.add_candidate(name="Ana Ruiz")
    html = local_client(store).get("/").text
    assert "Nothing needs you today" in html and 'id="next-steps"' in html
    assert "Create your first job" in html and "Connect your AI app" in html
    assert "Import people" not in html  # there are people already
    monkeypatch.setattr(integrations, "claude_connected", lambda path=None: True)
    store.add_job("Backend Engineer", company="Acme")
    html = local_client(store).get("/").text
    assert "Create your first job" not in html and "Connect your AI app" not in html


def test_today_next_steps_offer_duplicates(store, monkeypatch):
    from talent import duplicates
    monkeypatch.setattr(integrations, "claude_connected", lambda path=None: True)
    monkeypatch.setattr(duplicates, "count", lambda st: 2)
    store.add_candidate(name="Ana Ruiz")
    html = local_client(store).get("/").text
    assert "Review 2 possible duplicates" in html and 'href="/candidates/duplicates"' in html.split('id="next-steps"')[1]


def test_locked_page_names_linux():
    t = (SRC / "templates" / "locked.html").read_text()
    assert "On Linux, open Talent Search from your applications menu, or run talent-search in a terminal." in t


def test_tour_wording_and_behaviour():
    body = {s["id"]: s["body"] for s in tour.STEPS}
    assert "exact files first" not in body["skills"] and "never overwritten" in body["skills"]
    assert "Test connection" in body["claude"] and "Connect Claude" in body["claude"]
    assert '" of " + steps.length' not in JS and '"Step " + (idx + 1)' in JS
    assert "box below" not in JS
    assert "askToEnd" in JS.split("function skip()")[1].split("\n")[0]  # skipping with demo people asks first
    assert "white-space: nowrap" in (SRC / "static" / "style.css").read_text().split(".foot a")[1].split("}")[0]


def test_replay_link_restarts_a_running_tour(store):
    tour.start(store, tour.FULL)
    tour.set_step(store, 4)
    c = local_client(store)
    c.get("/candidates?tour=short")
    assert tour.state(store)["step"] == 0 and tour.state(store)["active"]


def test_footer_and_job_header_wording(store):
    html = local_client(store).get("/").text
    assert "Runs on this computer. Nothing is sent anywhere." in html and "uses none of your data" not in html
    job = store.add_job("Backend Engineer", company="Acme")
    jid = job["id"] if isinstance(job, dict) else job
    assert "in the running" in local_client(store).get(f"/jobs/{jid}").text


def test_candidates_sort_line_is_one_clear_line(store):
    store.add_candidate(name="Ana Ruiz", tags=["python"])
    html = local_client(store).get("/candidates").text
    assert "Newest first. Nobody has a label color yet." in html and "Sorted by color." not in html
    html = local_client(store).get("/candidates", params={"q": "title:engineer -manager skil:go"}).text
    visible = html[:html.index('<details class="how-details"')]
    assert "How we read your search" in visible and "isn't a search prefix" in visible.replace("&#39;", "'")


def test_empty_candidates_page_hides_color_chrome(store):
    html = local_client(store).get("/candidates").text
    assert 'id="cand-empty"' in html and 'aria-label="Label colors"' not in html and "Sorted by" not in html
    assert 'id="bulk-form"' not in html


def test_search_tips_match_the_page_and_say_how_or_works():
    t = (SRC / "templates" / "_search_tips.html").read_text()
    assert "Under the search box" not in t and "cannot group" in t and "python java OR go" in t
