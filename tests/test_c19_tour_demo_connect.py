"""Cycle 19: the tour keeps the demo people to explore (Clear demo data at the top right clears them), and one
connect step with an AI app menu replaces the separate Claude and Codex steps."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from conftest import flash_of, local_client
from talent import tour
from talent.sample_data import sample_ids

JS = (Path(__file__).resolve().parents[1] / "src" / "talent" / "static" / "app.js").read_text(encoding="utf-8")


def tour_data(response):
    m = re.search(r'<div id="tour-data" hidden data-tour="([^"]*)"></div>', response.text)
    return json.loads(html.unescape(m.group(1))) if m else None


def connect_step(data):
    return next(s for s in data["steps"] if s["id"] == "connect")


def test_one_connect_step_offers_every_ai_app(store):
    c = local_client(store)
    data = tour_data(c.get("/"))
    ids = [s["id"] for s in data["steps"]]
    assert ids[:2] == ["welcome", "connect"] and "claude" not in ids and "codex" not in ids
    choices = connect_step(data)["choices"]
    assert [x["id"] for x in choices] == ["claude", "claude-code", "codex", "lmstudio", "other"]
    assert all(x["label"] and x["line"] and x["target"] for x in choices)


def test_every_choice_lights_up_a_panel_that_is_on_the_connect_page(store):
    c = local_client(store)
    c.get("/")
    c.post("/tour/step", data={"step": "1"})
    page = c.get("/connect")
    data = tour_data(page)
    assert data and data["app"] in {"claude", "codex", "lmstudio"}  # the menu starts on the app this computer has
    for choice in connect_step(data)["choices"]:
        panel = choice["target"][0]
        assert panel.startswith("#") and f'id="{panel[1:]}"' in page.text, panel
        if choice.get("open"):
            assert f'id="{choice["open"][1:]}"' in page.text


def test_the_popover_draws_the_menu_and_keeps_the_pick():
    assert 'sel.id = "tour-app"' in JS and "ts-tour-app" in JS
    assert "function aim(s)" in JS and "const s = aim(steps[idx]);" in JS  # the pick decides what is lit up
    assert "d.open = true" in JS  # Claude Code's steps open so its Copy button shows


def test_clear_demo_data_button_shows_only_while_there_is_demo_data(store):
    c = local_client(store)
    assert 'action="/sample-data/clear"' not in c.get("/settings").text
    assert 'action="/sample-data"' in c.get("/settings").text  # Settings can load them any time
    c.post("/sample-data", follow_redirects=False)
    page = c.get("/settings").text
    assert 'action="/sample-data/clear"' in page and "Clear demo data" in page
    assert flash_of(c.post("/sample-data/clear", follow_redirects=False)).startswith("Removed the demo data")
    assert sample_ids(store) == (set(), set()) and store.count_candidates() == 0


def test_clearing_never_touches_your_own_people_in_a_demo_pipeline(store):
    c = local_client(store)
    c.post("/sample-data", follow_redirects=False)
    mine = store.add_candidate(name="Jordan Pierce")
    job = next(iter(sample_ids(store)[1]))
    store.add_to_pipeline(mine["id"], job)
    c.post("/sample-data/clear", follow_redirects=False)
    assert [x["name"] for x in store.all_candidates()] == ["Jordan Pierce"] and not store.list_jobs()


def test_the_end_question_mentions_the_button_when_demo_people_are_here():
    assert "Clear demo data at the top right" in JS
    assert "Also delete" not in JS and "End tour and clear demo data" not in JS
    assert all("clears them" not in s["body"] for s in tour.STEPS)
